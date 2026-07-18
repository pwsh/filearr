// Package reconcile implements the agent (client) half of the P5-T5 full-manifest
// reconciliation sweep (brief §4.4). Periodically, or on a replication dead-end,
// the agent ships central a canonical digest of every active item under a root;
// on a mismatch it streams the full manifest so central can anti-join and correct
// drift WITHOUT the agent rebuilding its local index.
//
// # Digest canonicalization (FROZEN — must stay byte-identical to central)
//
// The digest is SHA-256 over compact, key-sorted JSON of the rows sorted by
// rel_path. Each row is {rel_path, size, mtime, quick_hash, content_hash} with
// keys sorted (content_hash, mtime, quick_hash, rel_path, size). Two boundaries
// are subtle and deliberately matched to the Python reference:
//
//   - mtime is serialized as an INTEGER number of MICROSECONDS, computed as
//     round(seconds*1e6) where seconds = float64(mtime_ns)/1e9. Python's round()
//     is round-half-to-EVEN, so Go uses math.RoundToEven (NOT math.Round, which
//     rounds half away from zero and diverges on exact .5 ties — and ties DO
//     occur: at a ~1.7e15 microsecond magnitude the float64 grid step is 0.25, so
//     x.5 is exactly representable). See manifest_test.go's subus_tie vector.
//   - strings use Python json ensure_ascii escaping: every rune outside the
//     printable ASCII range 0x20–0x7e (plus " and \) becomes \uXXXX (lowercase,
//     surrogate pair for non-BMP), so the two implementations agree regardless of
//     the host's default JSON escaping. Go's encoding/json emits raw UTF-8 and
//     escapes <,>,& — unusable here — hence the hand-rolled encoder below.
//
// NOTE for the central half: the agreed µs value is round(fsec*1e6) on the SAME
// float seconds the agent computes as float64(ns)/1e9. If central derives µs by
// round-tripping through a microsecond-precision datetime instead, confirm it
// yields the identical integer for tie values (it should, both are half-to-even),
// else digests diverge on sub-µs mtimes.
package reconcile

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"sort"
	"strconv"

	"github.com/filearr/filearr/agent/internal/index"
)

// Row is one manifest entry — the R1 field set (identity + size + mtime + hashes,
// nothing extracted). Empty QuickHash/ContentHash mean "absent" and serialize as
// JSON null in the digest (matching central's nullable ManifestRow).
type Row struct {
	RelPath     string
	Size        int64
	MtimeNs     int64 // local INTEGER unix-nanoseconds; converted per representation
	QuickHash   string
	ContentHash string
}

// mtimeSeconds is the wire (rows-endpoint) representation: float epoch seconds,
// the SAME value replication sends (outbox.nsToWireSeconds). Used for the JSON
// rows payload, NOT the digest.
func (r Row) mtimeSeconds() float64 { return float64(r.MtimeNs) / 1e9 }

// mtimeMicros is the DIGEST representation: integer microseconds, round-half-to-
// even of the float seconds, matching central's canonicalization exactly.
func (r Row) mtimeMicros() int64 {
	fsec := float64(r.MtimeNs) / 1e9
	return int64(math.RoundToEven(fsec * 1e6))
}

// ProjectItems maps local index items to manifest rows. Callers pass the active
// items for one root (index.Store.ActiveItems); this is a pure field projection.
func ProjectItems(items []*index.Item) []Row {
	rows := make([]Row, 0, len(items))
	for _, it := range items {
		rows = append(rows, Row{
			RelPath:     it.RelPath,
			Size:        it.Size,
			MtimeNs:     it.MtimeNs,
			QuickHash:   it.QuickHash,
			ContentHash: it.ContentHash,
		})
	}
	return rows
}

// Digest returns the canonical SHA-256 hex of a manifest. Rows are sorted by
// rel_path here (the caller need not pre-sort), so the digest is independent of
// projection/scan order — byte-identical to central over the same corpus.
func Digest(rows []Row) string {
	blob := canonicalJSON(rows)
	sum := sha256.Sum256(blob)
	return hex.EncodeToString(sum[:])
}

// canonicalJSON renders the frozen digest payload. It sorts by rel_path, sorts
// each row's keys, uses compact separators, and Python-ensure_ascii escaping.
func canonicalJSON(rows []Row) []byte {
	sorted := make([]Row, len(rows))
	copy(sorted, rows)
	sortByRelPath(sorted)

	var b []byte
	b = append(b, '[')
	for i, r := range sorted {
		if i > 0 {
			b = append(b, ',')
		}
		// Keys in sorted order: content_hash, mtime, quick_hash, rel_path, size.
		b = append(b, `{"content_hash":`...)
		b = appendNullableString(b, r.ContentHash)
		b = append(b, `,"mtime":`...)
		b = strconv.AppendInt(b, r.mtimeMicros(), 10)
		b = append(b, `,"quick_hash":`...)
		b = appendNullableString(b, r.QuickHash)
		b = append(b, `,"rel_path":`...)
		b = appendASCIIString(b, r.RelPath)
		b = append(b, `,"size":`...)
		b = strconv.AppendInt(b, r.Size, 10)
		b = append(b, '}')
	}
	b = append(b, ']')
	return b
}

// sortByRelPath sorts rows by rel_path. rel_path is unique per root
// (items(root_id, rel_path) UNIQUE), so ordering is total. The comparison is a
// plain Go string compare, which for valid UTF-8 equals a Unicode code-point
// compare — matching Python's sort of the str keys byte-for-byte.
func sortByRelPath(rows []Row) {
	sort.Slice(rows, func(i, j int) bool { return rows[i].RelPath < rows[j].RelPath })
}

// appendNullableString appends a JSON string, or the literal null when s == "".
func appendNullableString(b []byte, s string) []byte {
	if s == "" {
		return append(b, "null"...)
	}
	return appendASCIIString(b, s)
}

// appendASCIIString appends s as a JSON string using Python json's ensure_ascii
// escaping so the bytes match the reference digest exactly.
func appendASCIIString(b []byte, s string) []byte {
	b = append(b, '"')
	for _, r := range s {
		switch r {
		case '"':
			b = append(b, '\\', '"')
		case '\\':
			b = append(b, '\\', '\\')
		case '\b':
			b = append(b, '\\', 'b')
		case '\f':
			b = append(b, '\\', 'f')
		case '\n':
			b = append(b, '\\', 'n')
		case '\r':
			b = append(b, '\\', 'r')
		case '\t':
			b = append(b, '\\', 't')
		default:
			switch {
			case r >= 0x20 && r <= 0x7e:
				b = append(b, byte(r))
			case r <= 0xffff:
				b = appendU(b, uint16(r))
			default:
				// non-BMP: UTF-16 surrogate pair, matching Python ensure_ascii.
				rr := r - 0x10000
				hi := uint16(0xd800 + (rr >> 10))
				lo := uint16(0xdc00 + (rr & 0x3ff))
				b = appendU(b, hi)
				b = appendU(b, lo)
			}
		}
	}
	return append(b, '"')
}

const hexdigits = "0123456789abcdef"

// appendU appends a \uXXXX escape (lowercase hex), as Python json emits.
func appendU(b []byte, c uint16) []byte {
	return append(b, '\\', 'u',
		hexdigits[(c>>12)&0xf],
		hexdigits[(c>>8)&0xf],
		hexdigits[(c>>4)&0xf],
		hexdigits[c&0xf],
	)
}
