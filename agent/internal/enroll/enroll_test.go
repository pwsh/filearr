package enroll

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/smallstep/certificates/api"
	stepca "github.com/smallstep/certificates/ca"
)

// TestEnrollHappyPath drives the full handshake against a real in-process CA
// and a mock central: register -> OTT -> CSR/sign -> persist -> bind. It
// asserts the cert lands on disk and the fingerprint bound to central is the
// hex SHA-256 of the leaf DER, and that the leaf CN/SAN carry the agent_id.
func TestEnrollHappyPath(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	mc := newMockCentral(t, ca)
	tok := "fae_" + randToken()
	mc.addToken(tok)

	dir := t.TempDir()
	store := NewCertStore(dir)
	e := &Enroller{
		Central:  NewCentralClient(mc.URL()),
		Store:    store,
		Token:    tok,
		Hostname: "test-host",
		Platform: "linux",
		Name:     "test",
	}
	res, err := e.Enroll(context.Background())
	if err != nil {
		t.Fatalf("enroll: %v", err)
	}

	if res.CertFingerprint != mc.LastBindFingerprint {
		t.Fatalf("bound fingerprint %q != result %q", mc.LastBindFingerprint, res.CertFingerprint)
	}
	id, err := store.Load()
	if err != nil {
		t.Fatalf("load persisted identity: %v", err)
	}
	if got := CertFingerprint(id.Leaf); got != res.CertFingerprint {
		t.Fatalf("persisted leaf fingerprint %q != bound %q", got, res.CertFingerprint)
	}
	if id.Leaf.Subject.CommonName != res.AgentID {
		t.Fatalf("leaf CN %q != agent_id %q", id.Leaf.Subject.CommonName, res.AgentID)
	}
	if !containsStr(id.Leaf.DNSNames, res.AgentID) {
		t.Fatalf("leaf DNS SANs %v missing agent_id %q (verifies bare UUID -> DNS SAN)", id.Leaf.DNSNames, res.AgentID)
	}
	if len(id.Roots) == 0 || !id.Roots[0].Equal(ca.Root) {
		t.Fatalf("persisted roots did not include the CA root")
	}
	if id.State.AgentID != res.AgentID || id.State.CAURL != ca.URL {
		t.Fatalf("state sidecar mismatch: %+v", id.State)
	}
}

// TestEnrollNullOTTActionableError: central returns ca_ott == null (its
// provisioner JWK is unconfigured) -> enroll fails with an actionable error
// with honest recovery guidance (the token was consumed, so: fix the JWK, mint
// a NEW token, re-run enroll), and nothing is persisted.
func TestEnrollNullOTTActionableError(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	mc := newMockCentral(t, ca)
	mc.nilOTT = true
	tok := "fae_" + randToken()
	mc.addToken(tok)

	dir := t.TempDir()
	store := NewCertStore(dir)
	e := &Enroller{Central: NewCentralClient(mc.URL()), Store: store, Token: tok, Hostname: "h", Platform: "linux"}
	_, err := e.Enroll(context.Background())
	if !errors.Is(err, ErrCAOTTUnavailable) {
		t.Fatalf("expected ErrCAOTTUnavailable, got %v", err)
	}
	if !strings.Contains(err.Error(), "FILEARR_CA_PROVISIONER_JWK") {
		t.Fatalf("error should name the missing setting: %v", err)
	}
	if !strings.Contains(err.Error(), "NEW token") {
		t.Fatalf("error should say the consumed token needs re-minting: %v", err)
	}
	if _, lerr := store.Load(); lerr == nil {
		t.Fatalf("nothing should be persisted on a null-OTT failure")
	}
}

// TestReplayedOTTRejected: the same OTT signs once, then a replay is refused by
// the CA (jti single-use). Acceptance criterion.
func TestReplayedOTTRejected(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	client := caClientFor(t, ca)
	agentID := uuidV4()
	ott := ca.mintOTT(t, agentID)

	// First use succeeds.
	req1, _, err := stepca.CreateSignRequest(ott)
	if err != nil {
		t.Fatalf("create sign request: %v", err)
	}
	if _, err := client.Sign(req1); err != nil {
		t.Fatalf("first sign should succeed: %v", err)
	}
	// Replay of the same OTT is rejected.
	req2, _, err := stepca.CreateSignRequest(ott)
	if err != nil {
		t.Fatalf("create sign request (replay): %v", err)
	}
	if _, err := client.Sign(req2); err == nil {
		t.Fatalf("replayed OTT should be rejected by the CA")
	}
}

// TestExpiredOTTRejected: an OTT whose exp is in the past is refused. Acceptance
// criterion.
func TestExpiredOTTRejected(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	client := caClientFor(t, ca)
	agentID := uuidV4()
	past := time.Now().Add(-10 * time.Minute)
	ott := ca.mintOTT(t, agentID, ottOpts{iat: past, nbf: past, exp: past.Add(1 * time.Minute)})

	req, _, err := stepca.CreateSignRequest(ott)
	if err != nil {
		t.Fatalf("create sign request: %v", err)
	}
	if _, err := client.Sign(req); err == nil {
		t.Fatalf("expired OTT should be rejected")
	}
}

// TestSANMismatchRejected: a hand-built CSR carrying a SAN not present in the
// OTT sans is refused. This VERIFIES the spike's flagged assumption that
// step-ca enforces CSR SANs == OTT sans (newDefaultSANsValidator).
func TestSANMismatchRejected(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	client := caClientFor(t, ca)
	agentID := uuidV4()
	ott := ca.mintOTT(t, agentID) // sans == [agentID]

	// Build a CSR whose SAN is NOT the agent_id.
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	tmpl := &x509.CertificateRequest{
		Subject:  pkix.Name{CommonName: agentID},
		DNSNames: []string{"attacker.example.com"},
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, tmpl, key)
	if err != nil {
		t.Fatalf("create csr: %v", err)
	}
	csr, err := x509.ParseCertificateRequest(der)
	if err != nil {
		t.Fatalf("parse csr: %v", err)
	}
	req := &api.SignRequest{CsrPEM: api.CertificateRequest{CertificateRequest: csr}, OTT: ott}
	if _, err := client.Sign(req); err == nil {
		t.Fatalf("CSR with SAN != OTT sans should be rejected")
	}
}

// TestClockSkewTolerance probes step-ca's leeway on a future nbf/iat. step-ca's
// JWK provisioner validates with a 1-minute leeway (jwk.authorizeToken), so a
// +30s skew is tolerated while a well-past-leeway skew is rejected. Recorded
// here because the spike flagged this unverified.
func TestClockSkewTolerance(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	client := caClientFor(t, ca)

	t.Run("within_leeway_30s_accepted", func(t *testing.T) {
		agentID := uuidV4()
		future := time.Now().Add(30 * time.Second)
		ott := ca.mintOTT(t, agentID, ottOpts{iat: future, nbf: future})
		req, _, err := stepca.CreateSignRequest(ott)
		if err != nil {
			t.Fatalf("create sign request: %v", err)
		}
		if _, err := client.Sign(req); err != nil {
			t.Fatalf("+30s skew should be within step-ca's 1m leeway, got: %v", err)
		}
	})

	t.Run("beyond_leeway_2m_rejected", func(t *testing.T) {
		agentID := uuidV4()
		future := time.Now().Add(2 * time.Minute)
		ott := ca.mintOTT(t, agentID, ottOpts{iat: future, nbf: future})
		req, _, err := stepca.CreateSignRequest(ott)
		if err != nil {
			t.Fatalf("create sign request: %v", err)
		}
		if _, err := client.Sign(req); err == nil {
			t.Fatalf("+2m skew is beyond the 1m leeway and should be rejected")
		}
	})
}

// --- helpers ---------------------------------------------------------------

func caClientFor(t *testing.T, ca *testCA) *stepca.Client {
	t.Helper()
	c, err := stepca.NewClient(ca.URL, stepca.WithRootSHA256(ca.RootSHA256))
	if err != nil {
		t.Fatalf("build CA client: %v", err)
	}
	return c
}

func containsStr(ss []string, want string) bool {
	for _, s := range ss {
		if s == want {
			return true
		}
	}
	return false
}
