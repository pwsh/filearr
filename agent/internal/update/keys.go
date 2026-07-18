package update

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"strings"
)

// PublicKeyBase64 is the release-signing public key pinned into the agent binary
// at BUILD time. The default is EMPTY: a binary built without the pin cannot
// verify any manifest and therefore refuses every update (fail-closed). A real
// deployment overrides it at link time:
//
//	go build -ldflags "-X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=<base64-pubkey>" \
//	    ./cmd/filearr-agent
//
// The <base64-pubkey> value is what `filearr-release keygen` prints. The private
// key NEVER lives in the repo or on the central server — it stays on the
// operator's signing machine (research §8: keeps "central compromised" from
// implying "attacker can push a malicious agent update").
var PublicKeyBase64 = ""

// PinnedKey returns the decoded pinned public key, or ErrNoPinnedKey when the
// binary was built without a (valid) pin.
func PinnedKey() (ed25519.PublicKey, error) {
	return DecodePublicKey(PublicKeyBase64)
}

// DecodePublicKey decodes a base64 std-encoded Ed25519 public key. An empty or
// malformed value yields ErrNoPinnedKey / a wrapped decode error, never a
// zero-length key that would silently accept anything.
func DecodePublicKey(b64 string) (ed25519.PublicKey, error) {
	s := strings.TrimSpace(b64)
	if s == "" {
		return nil, ErrNoPinnedKey
	}
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("update: decode pinned public key: %w", err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("update: pinned public key wrong size: got %d, want %d", len(raw), ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(raw), nil
}
