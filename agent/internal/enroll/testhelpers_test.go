package enroll

import (
	"crypto/rand"
	"encoding/hex"
	"testing"

	caconfig "github.com/smallstep/certificates/authority/config"
)

// loadCAConfig parses a ca.json into the smallstep config the authority expects.
func loadCAConfig(path string) (*caconfig.Config, error) {
	return caconfig.LoadConfiguration(path)
}

// randHex returns n random bytes hex-encoded (used for jti / uuid-ish ids).
func randHex(t *testing.T, n int) string {
	t.Helper()
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		t.Fatalf("rand: %v", err)
	}
	return hex.EncodeToString(b)
}

// uuidV4 returns a UUID-v4-shaped string (central assigns a real UUID; tests
// only need a stable DNS-SAN-safe identifier).
func uuidV4() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return hex.EncodeToString(b[0:4]) + "-" + hex.EncodeToString(b[4:6]) + "-" +
		hex.EncodeToString(b[6:8]) + "-" + hex.EncodeToString(b[8:10]) + "-" +
		hex.EncodeToString(b[10:16])
}

// newTestAgentIDFor returns a fresh UUID-shaped agent id (token param is
// accepted for call-site readability; central assigns ids independent of it).
func newTestAgentIDFor(string) string { return uuidV4() }

// randToken returns a short random hex string for one-off secrets/tokens.
func randToken() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
