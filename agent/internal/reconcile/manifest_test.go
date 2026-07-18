package reconcile

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// Parity fixtures: the expected digests are precomputed by the Python reference
// canonicalization (mtime = round(seconds*1e6) integer microseconds, banker's
// rounding == Go math.RoundToEven; rows sorted by rel_path; per-row keys sorted;
// compact separators; ensure_ascii; SHA-256 hex). Regenerate with:
//
//	python gen_fixtures.py (any Python with xxhash installed)
//
// whose canon() is, verbatim:
//
//	payload=[{"rel_path":r.rel_path,"size":r.size,"mtime":round(r.mtime_ns/1e9*1e6),
//	          "quick_hash":r.quick_hash,"content_hash":r.content_hash}
//	         for r in sorted(rows, key=lambda r:r.rel_path)]
//	blob=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=True)
//	sha256(blob.encode()).hexdigest()

// unicodeA exercises ensure_ascii escaping of quote, backslash, TAB and Latin-1
// accents; unicodeB exercises a non-BMP surrogate pair (😀).
const (
	unicodeA = "música/naïve \"quote\"\\slash\t.mp3"
	unicodeB = "emoji/\U0001F600.mkv"
)

func TestDigestParityWithPythonReference(t *testing.T) {
	cases := []struct {
		name string
		rows []Row
		want string
	}{
		{
			name: "empty",
			rows: nil,
			want: "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
		},
		{
			name: "basic",
			rows: []Row{
				{RelPath: "b/movie.mkv", Size: 1048576, MtimeNs: 1700000000123456789, QuickHash: "qh1", ContentHash: "ch1"},
				{RelPath: "a/song.flac", Size: 2048, MtimeNs: 1699999999000000000, QuickHash: "qh2"},
			},
			want: "136aac7916cfa9922991ab3c27f01cb18147de4a504f7f8fa58e83e3131e066c",
		},
		{
			name: "nilhashes",
			rows: []Row{{RelPath: "only.txt", Size: 0, MtimeNs: 0}},
			want: "45bc840a3f950b020b6f21e7af74b8ca3bbcf65c85d50362707bc17c1cf4024b",
		},
		{
			// mtime_ns whose seconds*1e6 lands exactly on x.5 — banker's rounding
			// (RoundToEven) rounds DOWN to the even integer. math.Round would round
			// UP and diverge from central; this vector guards that.
			name: "subus_tie",
			rows: []Row{{RelPath: "tie.bin", Size: 123, MtimeNs: 1700000000000000384}},
			want: "15b7655ac9de630b5607e523ebac73ed2d186d1cf196b2c10ca9492dcb47c541",
		},
		{
			name: "unicode",
			rows: []Row{
				{RelPath: unicodeA, Size: 555, MtimeNs: 1650000000500000000, QuickHash: "q"},
				{RelPath: unicodeB, Size: 777, MtimeNs: 1650000000999999999, ContentHash: "c"},
			},
			want: "8b57136a073ccb51ed6dea5fd174922aba23547b097e77d95957c489e7d4a171",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Digest(tc.rows); got != tc.want {
				t.Errorf("Digest = %s\n   want %s\n   blob %s", got, tc.want, canonicalJSON(tc.rows))
			}
		})
	}
}

func TestDigestIsOrderIndependent(t *testing.T) {
	forward := []Row{
		{RelPath: "a/song.flac", Size: 2048, MtimeNs: 1699999999000000000, QuickHash: "qh2"},
		{RelPath: "b/movie.mkv", Size: 1048576, MtimeNs: 1700000000123456789, QuickHash: "qh1", ContentHash: "ch1"},
	}
	reversed := []Row{forward[1], forward[0]}
	if Digest(forward) != Digest(reversed) {
		t.Error("Digest must be independent of input row order (it sorts by rel_path)")
	}
}

func TestSubMicrosecondTieRoundsHalfToEven(t *testing.T) {
	// The tie value rounds to the even integer (down), matching Python round().
	r := Row{MtimeNs: 1700000000000000384}
	if got := r.mtimeMicros(); got != 1700000000000000 {
		t.Errorf("mtimeMicros tie = %d, want 1700000000000000 (half-to-even)", got)
	}
}

// --- projection: excludes missing, includes sidecars -----------------------

func openStore(t *testing.T) *index.Store {
	t.Helper()
	st, err := index.Open(filepath.Join(t.TempDir(), "index.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func seedRoot(t *testing.T, st *index.Store, path string) string {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, err := index.EnsureRoot(ctx, tx, path)
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return id
}

func seedItem(t *testing.T, st *index.Store, rootID, rel, status string, sidecar bool) {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := index.NewID()
	it := &index.Item{
		ID: id, RootID: rootID, RelPath: rel, Filename: filepath.Base(rel),
		Extension: "mkv", Size: 10, MtimeNs: time.Now().UnixNano(),
		QuickHash: "q", MediaType: "video", Status: status, IsSidecar: sidecar,
		FirstSeen: time.Now(), LastSeen: time.Now(),
	}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}

func TestProjectionExcludesMissingIncludesSidecars(t *testing.T) {
	st := openStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "Movies/active.mkv", index.StatusActive, false)
	seedItem(t, st, rid, "Movies/active.nfo", index.StatusActive, true)    // sidecar: INCLUDED
	seedItem(t, st, rid, "Movies/gone.mkv", index.StatusMissing, false)    // tombstone: EXCLUDED
	seedItem(t, st, rid, "Movies/trashed.mkv", index.StatusTrashed, false) // EXCLUDED

	items, err := st.ActiveItems(context.Background(), rid)
	if err != nil {
		t.Fatal(err)
	}
	rows := ProjectItems(items)
	got := map[string]bool{}
	for _, r := range rows {
		got[r.RelPath] = true
	}
	if len(rows) != 2 {
		t.Fatalf("manifest should hold exactly the 2 active rows, got %d: %v", len(rows), got)
	}
	if !got["Movies/active.mkv"] || !got["Movies/active.nfo"] {
		t.Errorf("manifest must include the active file AND its sidecar, got %v", got)
	}
	if got["Movies/gone.mkv"] || got["Movies/trashed.mkv"] {
		t.Errorf("manifest must exclude missing/trashed rows, got %v", got)
	}
}
