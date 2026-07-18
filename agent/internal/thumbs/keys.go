// Package thumbs is the agent-side port of Phase 12's thumbnail pipeline
// (P12-T13): content-addressed key derivation IDENTICAL to central, pure-Go
// generators (CGO_ENABLED=0 fleet — no libvips/govips), and a post-scan pass that
// pushes generated thumbnails to central's agent-plane small-blob endpoint.
//
// Two agents holding the same file by content hash derive the SAME cache key and
// so dedup to ONE central thumbnail (write-if-absent). The key is a byte-for-byte
// port of backend/filearr/thumbs.py:cache_key — parity is pinned in keys_test.go
// against values central actually computes.
//
// ENCODE-FORMAT DEVIATION (documented): central encodes WebP (Pillow). Under
// CGO_ENABLED=0 there is no production-grade pure-Go LOSSY WebP encoder (the byte
// caps rule out lossless VP8L for photos), so the agent encodes JPEG instead. The
// bytes are still stored under central's "<key>.webp" storage name (so central's
// abs_path / orphan-GC address them unchanged); central's serve path sniffs the
// magic bytes and sets the correct Content-Type. The KEY itself derives only from
// (hash, generator_version, tier) — never the encoded format — so JPEG-vs-WebP
// never affects addressing or dedup.
package thumbs

import (
	"encoding/hex"
	"fmt"

	"golang.org/x/crypto/blake2b"
)

// GeneratorVersion MUST match central's FILEARR_THUMBNAIL_GENERATOR_VERSION
// (backend/filearr/config.py thumbnail_generator_version). It is baked into every
// cache key, so a drift would make the agent write thumbnails under keys central
// never looks up. Bump ONLY in lockstep with central.
const GeneratorVersion = 1

// Tier identifiers — must equal backend/filearr/thumbs.py TIER_GRID / TIER_PREVIEW
// (persisted as thumbnail_manifest.tier). The key derives from tier, so these
// values are part of the content-addressing contract.
const (
	TierGrid    = 0
	TierPreview = 1
)

// CacheKey ports backend/filearr/thumbs.py:cache_key byte-for-byte —
// blake2b(f"{hash}:{gen}:{tier}", digest_size=16) hex (32 lowercase hex chars).
// hashUsed is the item's content_hash (fallback quick_hash) exactly as central
// keys it, so an agent and central (or two agents) holding the same file by hash
// address the SAME thumbnail. Parity is pinned in keys_test.go against central.
func CacheKey(hashUsed string, generatorVersion, tier int) string {
	basis := fmt.Sprintf("%s:%d:%d", hashUsed, generatorVersion, tier)
	// blake2b.New(16, nil) errors only on an out-of-range size / bad key; 16 is
	// always valid, so the error is structurally impossible here.
	h, _ := blake2b.New(16, nil)
	_, _ = h.Write([]byte(basis))
	return hex.EncodeToString(h.Sum(nil))
}

// FanoutPath ports backend/filearr/thumbs.py:fanout_path — the git-style 2-level
// relative path "ab/cd/<key>.webp". The ".webp" suffix is central's STORAGE
// naming (kept identical so central's abs_path/GC address the agent's blob
// unchanged), independent of the actual encoded format of the bytes (see the
// package doc). A non-hex key is rejected (traversal-proof by construction: a hex
// digest can hold no "/" or "..").
func FanoutPath(key string) (string, error) {
	if len(key) < 4 || !isLowerHex(key) {
		return "", fmt.Errorf("cache key must be a lowercase hex digest, got %q", key)
	}
	return fmt.Sprintf("%s/%s/%s.webp", key[:2], key[2:4], key), nil
}

func isLowerHex(s string) bool {
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return len(s) > 0
}
