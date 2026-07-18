// Package update implements the agent self-updater (P5-T7): fetching a
// signed release manifest from central, verifying its Ed25519 signature against
// a build-time pinned public key, downloading + sha256-verifying the matching
// per-platform artifact, performing an OS-appropriate A/B binary swap, and a
// crash-loop boot-counting rollback state machine.
//
// Trust model (research §8): central is UNTRUSTED for update integrity — it
// stores and serves the manifest (including its signature) but cannot re-sign
// it (the private key never leaves the operator's signing machine). The agent
// verifies the signature itself, so a compromised central cannot push a
// wrongly-signed binary. minisign-style single-keypair signing; full TUF was
// explicitly rejected (research §5.1).
package update

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
)

// Sentinel errors surfaced by manifest verification. The updater treats every
// one of these as "refuse the update" — a manifest that does not verify is
// never applied (accept criterion: an invalid signature is refused).
var (
	// ErrNoPinnedKey — no valid pinned public key is available (empty/malformed
	// -ldflags pin). The agent CANNOT verify updates and refuses all of them.
	ErrNoPinnedKey = errors.New("update: no valid pinned public key")
	// ErrUnsigned — the manifest carries no signature field.
	ErrUnsigned = errors.New("update: manifest is unsigned")
	// ErrBadSignature — the Ed25519 signature does not verify over the canonical
	// manifest bytes for the pinned key.
	ErrBadSignature = errors.New("update: manifest signature verification failed")
)

// Artifact is one built binary for a (platform, arch) pair. “URL“ is a plain
// filename (the manifest is served relative to central's per-release artifact
// endpoint — never an absolute URL an attacker could redirect); the agent
// resolves it against “{central}/api/v1/agents/{id}/releases/{version}/artifacts/{url}“.
type Artifact struct {
	Platform string `json:"platform"` // windows | macos | linux
	Arch     string `json:"arch"`     // amd64 | arm64 | ...
	SHA256   string `json:"sha256"`   // lowercase hex of the artifact bytes
	Size     int64  `json:"size"`     // byte length (informational + a cheap pre-check)
	URL      string `json:"url"`      // artifact FILENAME (no path separators)
}

// Manifest is the signed release document. The signature covers the CANONICAL
// serialization of {version, created_at, artifacts} (see canonicalBytes) —
// NOT the “signature“ field itself, and NOT the exact on-disk/JSONB bytes.
// Recomputing the canonical form from parsed fields makes verification robust
// to central storing the manifest as JSONB (which re-serializes it): the agent
// re-derives the signed bytes deterministically rather than trusting central's
// byte layout.
type Manifest struct {
	Version   string     `json:"version"`
	CreatedAt string     `json:"created_at"` // RFC3339 UTC
	Artifacts []Artifact `json:"artifacts"`
	Signature string     `json:"signature,omitempty"` // base64 std of the 64-byte Ed25519 sig
}

// canonicalBytes returns the deterministic bytes the signature is computed over.
//
// Canonicalization rules (documented so any re-implementation — the Python
// central, a future tool — matches byte-for-byte):
//   - a fixed top-level key order: version, created_at, artifacts (the
//     “signature“ field is EXCLUDED);
//   - artifacts sorted by (platform, arch, url) ascending;
//   - each artifact's keys in a fixed order: platform, arch, sha256, size, url;
//   - compact JSON (no insignificant whitespace), HTML escaping DISABLED
//     (so “<“ “>“ “&“ in a filename are literal), UTF-8, no trailing
//     newline.
func (m Manifest) canonicalBytes() ([]byte, error) {
	arts := append([]Artifact(nil), m.Artifacts...)
	sort.Slice(arts, func(i, j int) bool {
		if arts[i].Platform != arts[j].Platform {
			return arts[i].Platform < arts[j].Platform
		}
		if arts[i].Arch != arts[j].Arch {
			return arts[i].Arch < arts[j].Arch
		}
		return arts[i].URL < arts[j].URL
	})
	// A local struct type fixes the key order independent of Manifest's layout.
	canon := struct {
		Version   string     `json:"version"`
		CreatedAt string     `json:"created_at"`
		Artifacts []Artifact `json:"artifacts"`
	}{Version: m.Version, CreatedAt: m.CreatedAt, Artifacts: arts}

	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(canon); err != nil {
		return nil, fmt.Errorf("canonicalize manifest: %w", err)
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// Sign computes the base64 Ed25519 signature over the canonical manifest bytes.
// Used by the release tool; never by the agent.
func Sign(m Manifest, priv ed25519.PrivateKey) (string, error) {
	b, err := m.canonicalBytes()
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(ed25519.Sign(priv, b)), nil
}

// Verify checks the manifest signature against pub. It returns nil ONLY when a
// valid pinned key is present AND the signature verifies over the canonical
// bytes. Any other outcome is one of the sentinel errors above (all "refuse").
func Verify(m Manifest, pub ed25519.PublicKey) error {
	if len(pub) != ed25519.PublicKeySize {
		return ErrNoPinnedKey
	}
	if m.Signature == "" {
		return ErrUnsigned
	}
	sig, err := base64.StdEncoding.DecodeString(m.Signature)
	if err != nil {
		return fmt.Errorf("update: decode signature: %w", err)
	}
	b, err := m.canonicalBytes()
	if err != nil {
		return err
	}
	if !ed25519.Verify(pub, b, sig) {
		return ErrBadSignature
	}
	return nil
}

// ParseManifest decodes a manifest document (central's response body).
func ParseManifest(data []byte) (Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return Manifest{}, fmt.Errorf("update: parse manifest: %w", err)
	}
	return m, nil
}

// Marshal renders the signed manifest as indented JSON (release-tool output).
func Marshal(m Manifest) ([]byte, error) {
	return json.MarshalIndent(m, "", "  ")
}

// FindArtifact returns the artifact matching (platform, arch), or false.
func (m Manifest) FindArtifact(platform, arch string) (Artifact, bool) {
	for _, a := range m.Artifacts {
		if a.Platform == platform && a.Arch == arch {
			return a, true
		}
	}
	return Artifact{}, false
}
