package enroll

import (
	"context"
	"errors"
	"testing"
)

// TestCentralRegisterContract exercises the register JSON contract and the
// single-use token semantics against the mock central.
func TestCentralRegisterContract(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	mc := newMockCentral(t, ca)
	tok := "fae_" + randToken()
	mc.addToken(tok)

	c := NewCentralClient(mc.URL())
	resp, err := c.Register(context.Background(), RegisterRequest{
		Token: tok, Hostname: "nas", Platform: "linux", Name: "media", AgentVersion: "test",
	})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if resp.Status != "pending" || resp.EnrollSecret == "" || resp.AgentID == "" {
		t.Fatalf("unexpected register response: %+v", resp)
	}
	if resp.CA.URL != ca.URL || resp.CA.Fingerprint != ca.RootSHA256 {
		t.Fatalf("CA bootstrap mismatch: %+v", resp.CA)
	}
	if resp.CaOTT == nil || *resp.CaOTT == "" {
		t.Fatalf("expected a non-null ca_ott")
	}

	// Single-use: replaying the same token is a 401.
	_, err = c.Register(context.Background(), RegisterRequest{Token: tok, Hostname: "evil", Platform: "linux"})
	var he *HTTPError
	if !errors.As(err, &he) || he.Status != 401 {
		t.Fatalf("expected 401 HTTPError on token replay, got %v", err)
	}
}

// TestCentralBindContract exercises the certificate-bind contract: a good
// secret binds (pending->active), the secret is single-use, and a bad secret is
// refused.
func TestCentralBindContract(t *testing.T) {
	ca := newTestCA(t, defaultCAParams())
	mc := newMockCentral(t, ca)
	tok := "fae_" + randToken()
	mc.addToken(tok)

	c := NewCentralClient(mc.URL())
	reg, err := c.Register(context.Background(), RegisterRequest{Token: tok, Hostname: "h", Platform: "linux"})
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	// Wrong secret -> 401.
	_, err = c.BindCertificate(context.Background(), reg.AgentID, BindRequest{EnrollSecret: "nope", CertFingerprint: "abc"})
	var he *HTTPError
	if !errors.As(err, &he) || he.Status != 401 {
		t.Fatalf("expected 401 on bad secret, got %v", err)
	}

	// Correct secret binds.
	ag, err := c.BindCertificate(context.Background(), reg.AgentID, BindRequest{EnrollSecret: reg.EnrollSecret, CertFingerprint: "fp-1"})
	if err != nil {
		t.Fatalf("bind: %v", err)
	}
	if ag.Status != "active" || ag.CertFingerprint != "fp-1" {
		t.Fatalf("unexpected agent after bind: %+v", ag)
	}

	// Idempotent re-bind of the SAME fingerprint is accepted.
	if _, err := c.BindCertificate(context.Background(), reg.AgentID, BindRequest{EnrollSecret: reg.EnrollSecret, CertFingerprint: "fp-1"}); err != nil {
		t.Fatalf("idempotent re-bind should succeed: %v", err)
	}
	// A DIFFERENT fingerprint after binding -> 409.
	_, err = c.BindCertificate(context.Background(), reg.AgentID, BindRequest{EnrollSecret: reg.EnrollSecret, CertFingerprint: "fp-2"})
	if !errors.As(err, &he) || he.Status != 409 {
		t.Fatalf("expected 409 on rebinding a different fingerprint, got %v", err)
	}
}
