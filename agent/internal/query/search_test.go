package query

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// seed builds a temp index with a fixed corpus and returns its path. now anchors
// the mtime/first_seen values so relative-time filters are deterministic.
func seed(t *testing.T, now time.Time) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	tx, _ := st.Begin(ctx)
	rid, err := index.EnsureRoot(ctx, tx, "/media")
	if err != nil {
		t.Fatal(err)
	}
	tx.Commit()

	type row struct {
		rel, ext, media, group, qhash, chash string
		size                                 int64
		ageDays                              int // mtime/first_seen = now - ageDays
		sidecar                              bool
	}
	rows := []row{
		{rel: "Movies/Arcane.S01E01.mkv", ext: "mkv", media: "video", group: "video", size: 2 * 1024 * 1024 * 1024, ageDays: 1, qhash: "aa11", chash: "cc11"},
		{rel: "Movies/Arcane.S01E01.nfo", ext: "nfo", media: "other", group: "other", size: 2048, ageDays: 1, sidecar: true},
		{rel: "Music/Song.flac", ext: "flac", media: "audio", group: "audio-lossless", size: 40 * 1024 * 1024, ageDays: 40},
		{rel: "Docs/Annual-Report-2025.pdf", ext: "pdf", media: "document", group: "pdf", size: 500 * 1024, ageDays: 10, qhash: "bb22"},
		{rel: "Docs/notes.txt", ext: "txt", media: "document", group: "document-text", size: 100, ageDays: 100},
		{rel: "Photos/beach.jpg", ext: "jpg", media: "image", group: "raster-photo", size: 3 * 1024 * 1024, ageDays: 3},
	}
	for _, r := range rows {
		tx, _ := st.Begin(ctx)
		id, _ := index.NewID()
		ts := now.Add(-time.Duration(r.ageDays) * 24 * time.Hour)
		it := &index.Item{
			ID: id, RootID: rid, RelPath: r.rel, Filename: filepath.Base(r.rel),
			Extension: r.ext, Size: r.size, MtimeNs: ts.UnixNano(),
			QuickHash: r.qhash, ContentHash: r.chash, FileCategory: r.media, FileGroup: r.group,
			Status: index.StatusActive, IsSidecar: r.sidecar, FirstSeen: ts, LastSeen: ts,
		}
		if err := index.InsertItem(ctx, tx, it); err != nil {
			t.Fatal(err)
		}
		tx.Commit()
	}
	st.Close()
	return path
}

func newSearcherAt(t *testing.T, path string, now time.Time) *Searcher {
	t.Helper()
	s, err := NewSearcher(path)
	if err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return now }
	t.Cleanup(func() { s.Close() })
	return s
}

func rels(rs []Result) []string {
	out := make([]string, len(rs))
	for i, r := range rs {
		out[i] = r.Item.RelPath
	}
	return out
}

func has(rs []Result, rel string) bool {
	for _, r := range rs {
		if r.Item.RelPath == rel {
			return true
		}
	}
	return false
}

func TestExecutionMatrix(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	path := seed(t, now)
	s := newSearcherAt(t, path, now)
	ctx := context.Background()

	cases := []struct {
		name    string
		q       string
		want    []string // rel_paths expected (order-insensitive membership)
		exactly bool     // if true, want must equal the full result set
	}{
		{name: "term", q: "arcane", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "kind_video", q: "kind:video", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "kind_document", q: "kind:document", want: []string{"Docs/Annual-Report-2025.pdf", "Docs/notes.txt"}, exactly: true},
		{name: "group_pdf", q: "group:pdf", want: []string{"Docs/Annual-Report-2025.pdf"}, exactly: true},
		{name: "group_audio_lossless", q: "group:audio-lossless", want: []string{"Music/Song.flac"}, exactly: true},
		{name: "ext_single", q: "ext:flac", want: []string{"Music/Song.flac"}, exactly: true},
		{name: "ext_list", q: "ext:jpg;flac", want: []string{"Photos/beach.jpg", "Music/Song.flac"}, exactly: true},
		{name: "size_gt_1G", q: "size:>1G", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "size_range_K", q: "size:100K..1M", want: []string{"Docs/Annual-Report-2025.pdf"}, exactly: true},
		{name: "size_lt_1K", q: "size:<1K", want: []string{"Docs/notes.txt"}, exactly: true},
		{name: "modified_within_7d", q: "modified:<7d", want: []string{"Movies/Arcane.S01E01.mkv", "Photos/beach.jpg"}, exactly: true},
		{name: "modified_older_30d", q: "modified:>30d", want: []string{"Music/Song.flac", "Docs/notes.txt"}, exactly: true},
		{name: "modified_date_after", q: "modified:>2026-07-10", want: []string{"Movies/Arcane.S01E01.mkv", "Photos/beach.jpg"}, exactly: true},
		{name: "created_within_7d", q: "created:<7d", want: []string{"Movies/Arcane.S01E01.mkv", "Photos/beach.jpg"}, exactly: true},
		{name: "path_glob", q: "path:Docs/*", want: []string{"Docs/Annual-Report-2025.pdf", "Docs/notes.txt"}, exactly: true},
		{name: "hash_quick", q: "hash:aa11", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "hash_content", q: "hash:cc11", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "negation_term", q: "-arcane kind:video", want: nil, exactly: true},
		{name: "quoted_term", q: `"annual-report"`, want: []string{"Docs/Annual-Report-2025.pdf"}, exactly: true},
		{name: "combo", q: "kind:video size:>1G modified:<7d", want: []string{"Movies/Arcane.S01E01.mkv"}, exactly: true},
		{name: "neg_ext", q: "kind:document -ext:txt", want: []string{"Docs/Annual-Report-2025.pdf"}, exactly: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := s.Search(ctx, tc.q, false, 100)
			if err != nil {
				t.Fatalf("search %q: %v", tc.q, err)
			}
			if tc.exactly && len(got) != len(tc.want) {
				t.Fatalf("q=%q got %v, want %v", tc.q, rels(got), tc.want)
			}
			for _, w := range tc.want {
				if !has(got, w) {
					t.Errorf("q=%q missing %q; got %v", tc.q, w, rels(got))
				}
			}
			for _, r := range got {
				if r.FuzzyMatched {
					t.Errorf("q=%q unexpected fuzzy match on %q", tc.q, r.Item.RelPath)
				}
			}
		})
	}
}

func TestSidecarDefaultExcluded(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	ctx := context.Background()

	got, err := s.Search(ctx, "arcane", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("sidecars must be hidden by default: got %v", rels(got))
	}
	got, _ = s.Search(ctx, "arcane", true, 10)
	if len(got) != 2 {
		t.Fatalf("--sidecars must include the .nfo: got %v", rels(got))
	}
}

func TestFuzzyZeroHitTrigger(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	ctx := context.Background()

	// "arcaen" (transposition) has zero exact/substring hits -> fuzzy re-rank fires.
	got, err := s.Search(ctx, "arcaen", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if !has(got, "Movies/Arcane.S01E01.mkv") {
		t.Fatalf("zero-hit fuzzy should recover Arcane: got %v", rels(got))
	}
	for _, r := range got {
		if !r.FuzzyMatched {
			t.Errorf("zero-hit recovery rows must be flagged fuzzy: %q", r.Item.RelPath)
		}
	}
}

func TestFuzzyExplicitTilde(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	ctx := context.Background()

	// Explicit ~ triggers fuzzy even though "song" would exact-match nothing else;
	// "sogn" is a typo for the "song" token in Song.flac.
	got, err := s.Search(ctx, "~sogn", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if !has(got, "Music/Song.flac") {
		t.Fatalf("explicit-~ fuzzy should recover Song.flac: got %v", rels(got))
	}
}

func TestFuzzyNotTriggeredOnExactHit(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	ctx := context.Background()

	// An exact hit exists (no ~), so the fuzzy layer must NOT engage and must not
	// pull in edit-distance neighbours.
	got, err := s.Search(ctx, "arcane", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range got {
		if r.FuzzyMatched {
			t.Fatalf("exact hit present -> fuzzy must not engage: %q", r.Item.RelPath)
		}
	}
}

func TestUnsupportedFilters(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	ctx := context.Background()

	for _, q := range []string{"tag:favorite", "meta.bitrate:>1000", "cf.rating:5"} {
		_, err := s.Search(ctx, q, false, 10)
		var ee *ExecError
		if !errors.As(err, &ee) || ee.Code != ErrUnsupportedFilter {
			t.Fatalf("q=%q expected unsupported_filter ExecError, got %v", q, err)
		}
		if len(ee.Keys) == 0 {
			t.Errorf("q=%q ExecError must list the offending key", q)
		}
	}
}

func TestUnknownKind(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	_, err := s.Search(context.Background(), "kind:hologram", false, 10)
	var ee *ExecError
	if !errors.As(err, &ee) || ee.Code != ErrUnknownKind {
		t.Fatalf("expected unknown_kind ExecError, got %v", err)
	}
}

func TestParseErrorSurfaces(t *testing.T) {
	now := time.Now()
	s := newSearcherAt(t, seed(t, now), now)
	_, err := s.Search(context.Background(), "size:1X", false, 10)
	var pe *ParseError
	if !errors.As(err, &pe) || pe.Code != "bad_size_suffix" {
		t.Fatalf("expected ParseError bad_size_suffix, got %v", err)
	}
}

// TestReadOnlyIsolation proves the Searcher's handle cannot write, and that a
// concurrent writer's committed rows become visible to the open read-only
// searcher (adversarial: no DSL input can coerce a write; brief §3.4).
func TestReadOnlyIsolation(t *testing.T) {
	now := time.Now()
	path := seed(t, now)
	s := newSearcherAt(t, path, now)
	ctx := context.Background()

	// The read-only connection rejects any direct write attempt.
	if _, err := s.db.ExecContext(ctx, "INSERT INTO items(id,root_id,rel_path,filename,status) VALUES('x','x','x','x','active')"); err == nil {
		t.Fatal("read-only searcher handle must reject writes")
	}

	// A separate writable Store inserts a new row WHILE the searcher is open...
	w, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	tx, _ := w.Begin(ctx)
	rid, _ := index.EnsureRoot(ctx, tx, "/media")
	id, _ := index.NewID()
	tsn := now.UnixNano()
	it := &index.Item{ID: id, RootID: rid, RelPath: "Late/zznew.mkv", Filename: "zznew.mkv",
		Extension: "mkv", Size: 5, MtimeNs: tsn, FileCategory: "video", Status: index.StatusActive,
		FirstSeen: now, LastSeen: now}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	// ...and the already-open read-only searcher observes it (WAL visibility).
	got, err := s.Search(ctx, "zznew", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if !has(got, "Late/zznew.mkv") {
		t.Fatalf("read-only searcher should see the writer's committed row: got %v", rels(got))
	}
}
