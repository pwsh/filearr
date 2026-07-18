package enroll

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"math/big"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/smallstep/certificates/api"
)

// errFakeTransient is returned by fakeRC for its leading failFor calls.
var errFakeTransient = errors.New("transient renew failure")

// --- pure scheduling helpers ----------------------------------------------

func TestRenewDelayTwoThirds(t *testing.T) {
	now := time.Date(2026, 7, 16, 12, 0, 0, 0, time.UTC)
	nb := now
	na := now.Add(48 * time.Hour)
	_, leaf := selfSignedValidity(t, "cn", nb, na)

	// Default jitter is 10% (a zero field means "unset" -> default), so assert
	// the delay lands in the jittered band centred on 2/3 of 48h = 32h.
	r := &Renewer{now: func() time.Time { return now }}
	d := r.renewDelay(leaf)
	want := 32 * time.Hour
	low := time.Duration(float64(want) * 0.90)
	high := time.Duration(float64(want) * 1.10)
	if d < low || d > high {
		t.Fatalf("renewDelay = %s, want within [%s, %s] (2/3 of lifetime +/-10%% jitter)", d, low, high)
	}
}

func TestRenewDelayPastExpiryIsZero(t *testing.T) {
	now := time.Now()
	nb := now.Add(-2 * time.Hour)
	na := now.Add(-time.Hour) // already expired
	_, leaf := selfSignedValidity(t, "cn", nb, na)
	r := &Renewer{now: func() time.Time { return now }}
	if d := r.renewDelay(leaf); d != 0 {
		t.Fatalf("expired cert should renew immediately (0), got %s", d)
	}
}

func TestNextBackoffDoublesAndCaps(t *testing.T) {
	max := 15 * time.Minute
	cur := 30 * time.Second
	seq := []time.Duration{}
	for i := 0; i < 8; i++ {
		cur = nextBackoff(cur, max)
		seq = append(seq, cur)
	}
	if seq[0] != time.Minute {
		t.Fatalf("first backoff = %s, want 1m", seq[0])
	}
	if last := seq[len(seq)-1]; last != max {
		t.Fatalf("backoff should cap at %s, got %s", max, last)
	}
}

func TestApplyJitterBounds(t *testing.T) {
	base := 100 * time.Second
	for i := 0; i < 1000; i++ {
		d := applyJitter(base, 0.10)
		if d < 90*time.Second || d >= 110*time.Second {
			t.Fatalf("jittered delay %s outside [90s,110s)", d)
		}
	}
}

// --- renewOnce with an injected fake client -------------------------------

// TestRenewOncePersistsNewCert: renewOnce swaps in the CA's returned leaf and
// refreshes roots while leaving the key untouched.
func TestRenewOncePersistsNewCert(t *testing.T) {
	dir := t.TempDir()
	store := NewCertStore(dir)
	key, leaf1 := selfSigned(t, "v1")
	_, root := selfSigned(t, "root")
	if err := store.SaveIdentity(Identity{Key: key, Leaf: leaf1, Roots: []*x509.Certificate{root}, State: State{AgentID: "a"}}); err != nil {
		t.Fatalf("save: %v", err)
	}

	_, leaf2 := selfSigned(t, "v2")
	fake := &fakeRC{
		resp: &api.SignResponse{
			ServerPEM:    api.NewCertificate(leaf2),
			CertChainPEM: []api.Certificate{api.NewCertificate(leaf2), api.NewCertificate(root)},
		},
		roots: &api.RootsResponse{Certificates: []api.Certificate{api.NewCertificate(root)}},
	}
	var renewed atomic.Int32
	r := &Renewer{
		Store:         store,
		clientFactory: func(_, _ string) (RenewClient, error) { return fake, nil },
		OnRenew:       func(*x509.Certificate) { renewed.Add(1) },
	}
	if err := r.renewOnce(context.Background()); err != nil {
		t.Fatalf("renewOnce: %v", err)
	}
	loaded, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if !loaded.Leaf.Equal(leaf2) {
		t.Fatalf("leaf was not replaced")
	}
	if renewed.Load() != 1 {
		t.Fatalf("OnRenew should have fired once, got %d", renewed.Load())
	}
}

// TestRunRetriesThenRecovers: Run backs off past a transient failure and then
// completes a renewal autonomously.
func TestRunRetriesThenRecovers(t *testing.T) {
	dir := t.TempDir()
	store := NewCertStore(dir)
	now := time.Now()
	// A leaf whose 2/3 point is already in the past -> Run renews immediately.
	key, leaf1 := selfSignedValidity(t, "v1", now.Add(-50*time.Second), now.Add(10*time.Second))
	_, root := selfSigned(t, "root")
	if err := store.SaveIdentity(Identity{Key: key, Leaf: leaf1, Roots: []*x509.Certificate{root}, State: State{AgentID: "a"}}); err != nil {
		t.Fatalf("save: %v", err)
	}
	_, leaf2 := selfSignedValidity(t, "v2", now.Add(-50*time.Second), now.Add(10*time.Second))
	fake := &fakeRC{
		resp:    &api.SignResponse{ServerPEM: api.NewCertificate(leaf2), CertChainPEM: []api.Certificate{api.NewCertificate(leaf2)}},
		roots:   &api.RootsResponse{Certificates: []api.Certificate{api.NewCertificate(root)}},
		failFor: 2,
	}
	done := make(chan struct{}, 1)
	r := &Renewer{
		Store:         store,
		BackoffMin:    5 * time.Millisecond,
		BackoffMax:    50 * time.Millisecond,
		clientFactory: func(_, _ string) (RenewClient, error) { return fake, nil },
		OnRenew: func(*x509.Certificate) {
			select {
			case done <- struct{}{}:
			default:
			}
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Run(ctx) }()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatalf("renewal never recovered after transient failures")
	}
	if got := atomic.LoadInt32(&fake.calls); got < 3 {
		t.Fatalf("expected at least 3 renew calls (2 fail + 1 ok), got %d", got)
	}
}

// --- real-CA renewal soak --------------------------------------------------

// TestRenewalSoak enrolls against a seconds-scale CA and asserts the renewal
// daemon completes >= 2 autonomous mTLS renewal cycles with no intervention.
func TestRenewalSoak(t *testing.T) {
	if testing.Short() {
		t.Skip("soak test skipped in -short mode")
	}
	ca := newTestCA(t, shortTTLCAParams())
	mc := newMockCentral(t, ca)
	tok := "fae_" + randToken()
	mc.addToken(tok)

	dir := t.TempDir()
	store := NewCertStore(dir)
	e := &Enroller{Central: NewCentralClient(mc.URL()), Store: store, Token: tok, Hostname: "soak", Platform: "linux"}
	if _, err := e.Enroll(context.Background()); err != nil {
		t.Fatalf("enroll: %v", err)
	}

	var count atomic.Int32
	got2 := make(chan struct{}, 1)
	r := &Renewer{
		Store:          store,
		CAURL:          ca.URL,
		RootSHA256:     ca.RootSHA256,
		RenewFraction:  0.34,
		JitterFraction: 0.05,
		BackoffMin:     200 * time.Millisecond,
		BackoffMax:     2 * time.Second,
		OnRenew: func(*x509.Certificate) {
			if count.Add(1) == 2 {
				select {
				case got2 <- struct{}{}:
				default:
				}
			}
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.Run(ctx) }()

	select {
	case <-got2:
	case <-time.After(40 * time.Second):
		t.Fatalf("only %d renewals completed; wanted >= 2", count.Load())
	}

	// The freshly renewed cert is valid and on disk.
	loaded, err := store.Load()
	if err != nil {
		t.Fatalf("load after soak: %v", err)
	}
	if time.Now().After(loaded.Leaf.NotAfter) {
		t.Fatalf("renewed cert is already expired")
	}
}

// --- helpers ---------------------------------------------------------------

// fakeRC is a fake RenewClient. The first failFor calls return an error, the
// rest return resp; Roots always returns roots.
type fakeRC struct {
	resp    *api.SignResponse
	roots   *api.RootsResponse
	failFor int32
	calls   int32
}

func (f *fakeRC) RenewWithContext(_ context.Context, _ http.RoundTripper) (*api.SignResponse, error) {
	n := atomic.AddInt32(&f.calls, 1)
	if n <= f.failFor {
		return nil, errFakeTransient
	}
	return f.resp, nil
}

func (f *fakeRC) Roots() (*api.RootsResponse, error) { return f.roots, nil }

func selfSignedValidity(t *testing.T, cn string, nb, na time.Time) (*ecdsa.PrivateKey, *x509.Certificate) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: cn},
		DNSNames:     []string{cn},
		NotBefore:    nb,
		NotAfter:     na,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create cert: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parse cert: %v", err)
	}
	return key, cert
}
