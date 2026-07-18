package enroll

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"time"

	"github.com/smallstep/certificates/api"
	"github.com/smallstep/certificates/ca"
)

// RenewClient is the subset of *ca.Client the renewal daemon needs.
// RenewWithContext performs the mTLS /renew (authenticated by the CURRENT cert
// carried on the supplied transport — no new OTT, spike verdict 4); Roots
// refreshes trust anchors after a CA rotation.
type RenewClient interface {
	RenewWithContext(ctx context.Context, tr http.RoundTripper) (*api.SignResponse, error)
	Roots() (*api.RootsResponse, error)
}

// Renewer runs the background certificate-renewal loop: it renews at ~2/3 of
// the cert lifetime (plus jitter), retries transient failures with capped
// exponential backoff, and honours the CA's allowRenewalAfterExpiry grace by
// still attempting a renew when the current cert is already (slightly) expired.
type Renewer struct {
	Store *CertStore
	CAURL string
	// RootSHA256 pins the CA root for the default client factory. The renew
	// transport itself trusts the persisted roots.pem (refreshed on rotation).
	RootSHA256 string

	Logger *slog.Logger

	// OnRenew, if set, is invoked with the freshly issued leaf after every
	// successful renewal. Used by tests/observability; must not block.
	OnRenew func(*x509.Certificate)

	// Tunables (zero values fall back to the constants below).
	RenewFraction  float64       // fraction of lifetime elapsed before renewing (default 2/3)
	JitterFraction float64       // +/- jitter applied to the scheduled delay (default 0.10)
	BackoffMin     time.Duration // initial retry backoff (default 30s)
	BackoffMax     time.Duration // backoff cap (default 15m)

	// Injectable seams for tests.
	now           func() time.Time
	clientFactory func(caURL, rootSHA256 string) (RenewClient, error)
}

const (
	defaultRenewFraction  = 2.0 / 3.0
	defaultJitterFraction = 0.10
	defaultBackoffMin     = 30 * time.Second
	defaultBackoffMax     = 15 * time.Minute
)

func (r *Renewer) logger() *slog.Logger {
	if r.Logger != nil {
		return r.Logger
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (r *Renewer) clock() time.Time {
	if r.now != nil {
		return r.now()
	}
	return time.Now()
}

func (r *Renewer) renewFraction() float64 {
	if r.RenewFraction > 0 && r.RenewFraction < 1 {
		return r.RenewFraction
	}
	return defaultRenewFraction
}

func (r *Renewer) jitterFraction() float64 {
	if r.JitterFraction > 0 {
		return r.JitterFraction
	}
	return defaultJitterFraction
}

func (r *Renewer) backoffMin() time.Duration {
	if r.BackoffMin > 0 {
		return r.BackoffMin
	}
	return defaultBackoffMin
}

func (r *Renewer) backoffMax() time.Duration {
	if r.BackoffMax > 0 {
		return r.BackoffMax
	}
	return defaultBackoffMax
}

// Run drives the renewal loop until ctx is cancelled, at which point it returns
// ctx.Err() for a clean shutdown. It never returns nil while ctx is live: a
// renewal failure is retried with backoff, not surfaced as a fatal error.
func (r *Renewer) Run(ctx context.Context) error {
	log := r.logger()
	backoff := r.backoffMin()
	failing := false

	for {
		leaf, err := r.currentLeaf()
		if err != nil {
			// No usable cert on disk is fatal for the daemon: nothing to renew.
			return fmt.Errorf("renewer: load current cert: %w", err)
		}

		var wait time.Duration
		if failing {
			wait = backoff
		} else {
			wait = r.renewDelay(leaf)
		}
		log.Debug("scheduling renewal", "wait", wait.String(), "not_after", leaf.NotAfter)

		if !sleepCtx(ctx, wait) {
			return ctx.Err()
		}

		if err := r.renewOnce(ctx); err != nil {
			failing = true
			log.Warn("renewal attempt failed; backing off", "backoff", backoff.String(), "err", err)
			backoff = nextBackoff(backoff, r.backoffMax())
			continue
		}
		failing = false
		backoff = r.backoffMin()
		log.Info("certificate renewed")
	}
}

// renewDelay computes how long to wait before renewing the given leaf: from now
// until the point ~renewFraction of the cert's lifetime has elapsed, plus
// bounded jitter. A cert already past that point (or expired) yields ~0 so the
// renew is attempted immediately (allowRenewalAfterExpiry grace path).
func (r *Renewer) renewDelay(leaf *x509.Certificate) time.Duration {
	lifetime := leaf.NotAfter.Sub(leaf.NotBefore)
	if lifetime <= 0 {
		return 0
	}
	target := leaf.NotBefore.Add(time.Duration(float64(lifetime) * r.renewFraction()))
	d := target.Sub(r.clock())
	if d < 0 {
		d = 0
	}
	return applyJitter(d, r.jitterFraction())
}

// renewOnce performs a single mTLS renewal: it presents the current cert on the
// transport, persists the new leaf+chain, and opportunistically refreshes the
// trusted roots (a CA rotation changes them).
func (r *Renewer) renewOnce(ctx context.Context) error {
	client, err := r.caClient()
	if err != nil {
		return err
	}
	tr, err := r.mtlsTransport()
	if err != nil {
		return err
	}
	// Ensure idle mTLS connections don't leak across renewals.
	defer closeIdle(tr)

	resp, err := client.RenewWithContext(ctx, tr)
	if err != nil {
		return fmt.Errorf("renew: %w", err)
	}
	leaf, chain, err := certsFromSignResponse(resp)
	if err != nil {
		return err
	}
	if err := r.Store.SaveCertificate(leaf, chain); err != nil {
		return fmt.Errorf("persist renewed cert: %w", err)
	}
	// Best-effort root refresh; a failure here must not fail the renewal since
	// the leaf is already safely persisted.
	if err := r.refreshRoots(client); err != nil {
		r.logger().Warn("root refresh after renewal failed", "err", err)
	}
	if r.OnRenew != nil {
		r.OnRenew(leaf)
	}
	return nil
}

// RefreshRoots fetches and persists the CA roots. Exposed so a config-push
// "CA rotated" signal (P5-T3+) can trigger a refresh out of band of the renewal
// schedule (spike verdict 4).
func (r *Renewer) RefreshRoots(ctx context.Context) error {
	client, err := r.caClient()
	if err != nil {
		return err
	}
	return r.refreshRoots(client)
}

func (r *Renewer) refreshRoots(client RenewClient) error {
	resp, err := client.Roots()
	if err != nil {
		return fmt.Errorf("fetch roots: %w", err)
	}
	roots := make([]*x509.Certificate, 0, len(resp.Certificates))
	for _, c := range resp.Certificates {
		if c.Certificate != nil {
			roots = append(roots, c.Certificate)
		}
	}
	if len(roots) == 0 {
		return nil
	}
	return r.Store.SaveRoots(roots)
}

func (r *Renewer) currentLeaf() (*x509.Certificate, error) {
	leaf, _, err := (&CertStore{Dir: r.Store.Dir}).loadCertChain()
	return leaf, err
}

// mtlsTransport builds the client transport that authenticates /renew: it
// presents the current leaf+key and trusts the persisted CA roots.
func (r *Renewer) mtlsTransport() (*http.Transport, error) {
	cert, err := r.Store.TLSCertificate()
	if err != nil {
		return nil, err
	}
	pool, err := r.Store.RootPool()
	if err != nil {
		return nil, err
	}
	return &http.Transport{
		TLSClientConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			RootCAs:      pool,
			MinVersion:   tls.VersionTLS12,
		},
	}, nil
}

func (r *Renewer) caClient() (RenewClient, error) {
	if r.clientFactory != nil {
		return r.clientFactory(r.CAURL, r.RootSHA256)
	}
	// The client's own transport (used by Roots()) trusts the persisted roots
	// so it keeps working across a CA rotation, rather than pinning a single
	// possibly-stale root SHA.
	pool, err := r.Store.RootPool()
	if err != nil {
		return nil, err
	}
	tr := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}
	client, err := ca.NewClient(r.CAURL, ca.WithTransport(tr))
	if err != nil {
		return nil, fmt.Errorf("build CA client: %w", err)
	}
	return client, nil
}

// --- helpers ---------------------------------------------------------------

// applyJitter returns d scaled by a uniform random factor in [1-frac, 1+frac].
func applyJitter(d time.Duration, frac float64) time.Duration {
	if d <= 0 || frac <= 0 {
		return d
	}
	// rand.Float64() in [0,1) -> factor in [1-frac, 1+frac).
	factor := 1 - frac + 2*frac*rand.Float64()
	return time.Duration(float64(d) * factor)
}

// nextBackoff doubles cur, capped at max.
func nextBackoff(cur, max time.Duration) time.Duration {
	next := cur * 2
	if next > max {
		return max
	}
	return next
}

// sleepCtx sleeps for d or until ctx is cancelled. Returns true if the full
// sleep elapsed, false if ctx was cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func closeIdle(tr *http.Transport) {
	if tr != nil {
		tr.CloseIdleConnections()
	}
}
