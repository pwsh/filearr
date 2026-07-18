// Package enroll implements the agent side of the register-first enrollment
// handshake (docs/ops/agents.md §3) and the background certificate-renewal
// daemon (docs/research/phase-5-t2a-stepca-spike.md). It talks to two peers:
// the central Filearr server (register + fingerprint-bind, plain JSON over the
// agent plane) and step-ca directly (CSR/sign + mTLS renew via the
// github.com/smallstep/certificates/ca client).
package enroll

import (
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
)

// CertFingerprint is the canonical fingerprint the agent reports to central's
// POST /agents/{id}/certificate. It is the lowercase hex SHA-256 of the leaf
// certificate's raw DER.
//
// Central stores whatever string it receives verbatim (see
// backend/filearr/agentsync.bind_agent_certificate — it only does an equality
// compare for idempotent re-bind; the P5-T1 tests bind an arbitrary "AA:BB").
// It imposes NO format. We deliberately adopt step-ca's own root-fingerprint
// convention — lowercase hex SHA-256 of the DER, the exact form
// ca.WithRootSHA256 / the /root/{sha} bootstrap endpoint use for the CA root —
// so an operator comparing an agent's cert_fingerprint against a
// `step certificate fingerprint <leaf>` output sees identical strings.
func CertFingerprint(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.Raw)
	return hex.EncodeToString(sum[:])
}
