package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/enroll"
)

// envAgentCABundle points at an optional PEM file of EXTRA trusted server roots,
// appended to the system pool. Needed when central's TLS chains to a root the
// host does not already trust (a private/internal issuer, an LE staging root, or
// the test CA in the local E2E). Unset => system roots only (public Let's Encrypt).
const envAgentCABundle = "FILEARR_AGENT_CA_BUNDLE"

// defaultHTTPTimeout matches the per-client defaults the outbox/policy/reconcile
// clients used before this shared seam existed.
const defaultHTTPTimeout = 30 * time.Second

// newHTTPClient builds the ONE HTTP client every agent->central client shares
// (replication, policy poll, reconcile). It is the mTLS seam (P5-T6):
//
//   - When central is https AND an enrolled leaf+key exist on disk, it presents
//     that client certificate so the Caddy agents.<domain> site (client_auth
//     require_and_verify) accepts the connection. The cert is loaded PER
//     HANDSHAKE from the cert store, so a renewed leaf is used without a restart.
//   - Server verification uses the system roots (public Let's Encrypt chains to
//     them), optionally augmented by FILEARR_AGENT_CA_BUNDLE.
//   - Plain-http central (dev / local E2E) returns a default client with no TLS
//     config — behaviour is unchanged there.
//
// Each client still sends the interim bearer token too (harmless; supports
// FILEARR_AGENT_AUTH_MODE=both during the migration window).
func newHTTPClient(store *enroll.CertStore, centralURL string) (*http.Client, error) {
	if !strings.HasPrefix(strings.ToLower(strings.TrimSpace(centralURL)), "https://") {
		return &http.Client{Timeout: defaultHTTPTimeout}, nil
	}

	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}

	if bundle := os.Getenv(envAgentCABundle); bundle != "" {
		pemBytes, err := os.ReadFile(bundle)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", envAgentCABundle, err)
		}
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pemBytes) {
			return nil, fmt.Errorf("%s (%s): no PEM certificates found", envAgentCABundle, bundle)
		}
		tlsCfg.RootCAs = pool
	}

	// Present the enrolled client cert when one exists. Loaded per-handshake so
	// renewal (new leaf, same key path) is picked up live. A missing/unreadable
	// identity yields an empty certificate (no client auth) — the server then
	// decides whether that is acceptable (it is not, for the agents.<domain>
	// require_and_verify site — the request fails, as it should).
	tlsCfg.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		pair, err := store.TLSCertificate()
		if err != nil {
			return &tls.Certificate{}, nil //nolint:nilerr // absent cert => no client auth, not a handshake error
		}
		return &pair, nil
	}

	return &http.Client{
		Timeout:   defaultHTTPTimeout,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}, nil
}
