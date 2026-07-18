package scan

import (
	"fmt"
	"io"
	"os"

	"github.com/zeebo/xxh3"
)

// quickChunk is the head/tail window size for QuickHash: 64 KiB. Mirrors
// backend/filearr/tasks/extract.py:QUICK_CHUNK.
const quickChunk = 65536

// HashPolicy controls content-hash computation for a scan. Mirrors central's
// resolved T7 policy narrowed to the two knobs the agent needs (ruling 6): quick
// hash is ALWAYS computed; content hash only when ComputeContent AND the file is
// at/below FullMaxBytes.
type HashPolicy struct {
	ComputeContent bool
	FullMaxBytes   int64
}

// DefaultFullMaxBytes mirrors central's global default
// FILEARR_SCAN_HASH_FULL_MAX_BYTES (config.py: scan_hash_full_max_bytes) = 1 GiB.
const DefaultFullMaxBytes int64 = 1 << 30

// DefaultHashPolicy computes both quick and content hashes with the central
// global size ceiling — the agent's local-by-construction default (no network
// quick_only downgrade, ruling 6).
func DefaultHashPolicy() HashPolicy {
	return HashPolicy{ComputeContent: true, FullMaxBytes: DefaultFullMaxBytes}
}

// QuickHash computes the xxh3_64 hex digest of a file's content as the fast
// move-detection probe. QH-T1 boundary edge (pinned identically to
// backend/filearr/tasks/extract.py:quick_hash): a file whose size <= 2*quickChunk
// (<=131072 bytes — INCLUSIVE of the 128 KiB point) is hashed IN FULL; only a
// file size > 2*quickChunk (strictly greater) is sampled as head 64 KiB + tail
// 64 KiB. The old code read a fixed 64 KiB head unconditionally and added the
// tail only above 131072, so a 64-128 KiB file had its middle+tail silently
// UNhashed (a false-duplicate defect). Byte-for-byte parity with the Python
// reference is enforced by hash_test.go against Python-precomputed digests.
// Default xxh3 seed; digest is the big-endian %016x form of Sum64 (matches
// Python xxhash .hexdigest()).
func QuickHash(pathStr string, size int64) (string, error) {
	f, err := os.Open(pathStr)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := xxh3.New()
	if size > quickChunk*2 {
		// >128 KiB: sampled head + tail (unchanged, by design).
		head := make([]byte, quickChunk)
		n, err := io.ReadFull(f, head)
		if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
			return "", err
		}
		if _, err := h.Write(head[:n]); err != nil {
			return "", err
		}
		if _, err := f.Seek(-quickChunk, io.SeekEnd); err != nil {
			return "", err
		}
		tail := make([]byte, quickChunk)
		m, err := io.ReadFull(f, tail)
		if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
			return "", err
		}
		if _, err := h.Write(tail[:m]); err != nil {
			return "", err
		}
	} else {
		// <=128 KiB: hash the WHOLE file. io.Copy sizes its own buffer to the data
		// (no fixed 64 KiB head cap) — cheap and correct for the small-file band.
		if _, err := io.Copy(h, f); err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("%016x", h.Sum64()), nil
}

// FullHash computes the xxh3_128 hex digest (32 lowercase hex chars, big-endian
// %016x of Hi then Lo) over the whole file (QH-T3: upgraded from xxh3_64 for a
// far larger collision margin at the same throughput). io.Copy picks a buffer
// sized for the reader, avoiding the fixed 1 MiB over-allocation the brief
// measured as most of the small-file cost (§5a). Parity with extract.full_hash
// is enforced by hash_test.go.
func FullHash(pathStr string) (string, error) {
	f, err := os.Open(pathStr)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := xxh3.New128()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	sum := h.Sum128()
	return fmt.Sprintf("%016x%016x", sum.Hi, sum.Lo), nil
}

// hashFile computes quick (always) and, when required, content hashes for one
// file. An OS error while hashing leaves both empty (the caller treats an
// unhashed row exactly as central does: it never matches a move and is re-queued
// by the next scan's self-heal). Mirrors move._ensure_hashes /
// extract.extract_item's hashing block.
//
// QH-T2: a file <= 2*quickChunk (128 KiB) ALWAYS gets a real content hash,
// independent of policy — it is cheap enough to hash exactly (§5a) and a sampled
// quick hash is never trustworthy identity for it. A larger file keeps the T7
// policy + ceiling gate.
func hashFile(pathStr string, size int64, policy HashPolicy) (quick, content string) {
	q, err := QuickHash(pathStr, size)
	if err != nil {
		return "", ""
	}
	quick = q
	if size <= quickChunk*2 || (policy.ComputeContent && size <= policy.FullMaxBytes) {
		if c, err := FullHash(pathStr); err == nil {
			content = c
		}
	}
	return quick, content
}
