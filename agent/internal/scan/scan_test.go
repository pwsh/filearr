package scan

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
)

func newStore(t *testing.T) *index.Store {
	t.Helper()
	st, err := index.Open(filepath.Join(t.TempDir(), "index.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func mustScan(t *testing.T, st *index.Store, opts Options) Result {
	t.Helper()
	res, err := Scan(context.Background(), st, opts)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	return res
}

func loadByRel(t *testing.T, st *index.Store, rootID string) map[string]*index.Item {
	t.Helper()
	m, err := st.LoadItems(context.Background(), rootID)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func TestScanNewChangedUnchangedMissingSelfHeal(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4", "b.mkv", "sub/c.flac"})
	st := newStore(t)

	// New.
	r1 := mustScan(t, st, Options{Root: root})
	if r1.New != 3 || r1.Changed != 0 || r1.Missing != 0 {
		t.Fatalf("first scan: %+v", r1)
	}
	items := loadByRel(t, st, r1.RootID)
	if items["a.mp4"].QuickHash == "" {
		t.Error("media file should be hashed inline")
	}
	if items["a.mp4"].Status != index.StatusActive {
		t.Error("new item should be active")
	}
	seqA := items["a.mp4"].LocalSeqNo

	// Unchanged rescan: healthy no-op (no counts, no seq churn).
	r2 := mustScan(t, st, Options{Root: root})
	if r2.New != 0 || r2.Changed != 0 || r2.Missing != 0 {
		t.Fatalf("unchanged rescan should be a no-op: %+v", r2)
	}
	if got := loadByRel(t, st, r1.RootID)["a.mp4"].LocalSeqNo; got != seqA {
		t.Errorf("unchanged healthy row must not churn local_seq_no: was %d, now %d", seqA, got)
	}

	// Changed: rewrite a.mp4 with a different size.
	if err := os.WriteFile(filepath.Join(root, "a.mp4"), []byte("much longer content"), 0o644); err != nil {
		t.Fatal(err)
	}
	r3 := mustScan(t, st, Options{Root: root})
	if r3.Changed != 1 || r3.New != 0 {
		t.Fatalf("expected 1 changed: %+v", r3)
	}

	// Missing: delete b.mkv -> tombstone.
	if err := os.Remove(filepath.Join(root, "b.mkv")); err != nil {
		t.Fatal(err)
	}
	r4 := mustScan(t, st, Options{Root: root})
	if r4.Missing != 1 {
		t.Fatalf("expected 1 missing: %+v", r4)
	}
	if loadByRel(t, st, r1.RootID)["b.mkv"].Status != index.StatusMissing {
		t.Error("deleted file should be tombstoned missing")
	}

	// Self-heal: recreate b.mkv -> active again.
	mktree(t, root, []string{"b.mkv"})
	mustScan(t, st, Options{Root: root})
	if loadByRel(t, st, r1.RootID)["b.mkv"].Status != index.StatusActive {
		t.Error("reappeared file should self-heal to active")
	}
}

func TestScanScopeMissingDoesNotTombstone(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"keep/a.mp4"})
	st := newStore(t)
	r1 := mustScan(t, st, Options{Root: root})
	if r1.New != 1 {
		t.Fatalf("%+v", r1)
	}
	// Scoped scan of a non-existent subtree: must write nothing, tombstone nothing.
	r2 := mustScan(t, st, Options{Root: root, StartRel: "not_here"})
	if !r2.ScopeMissing {
		t.Fatal("expected ScopeMissing")
	}
	if loadByRel(t, st, r1.RootID)["keep/a.mp4"].Status != index.StatusActive {
		t.Error("scope-missing scan must not tombstone out-of-scope items")
	}
}

func TestScanScopedWalkOnlyTouchesSubtree(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a/one.mp4", "b/two.mp4"})
	st := newStore(t)
	mustScan(t, st, Options{Root: root})
	// Remove b/two.mp4 but scan only scope a/: b's item must NOT be tombstoned
	// (a scoped walk cannot confirm out-of-scope items gone).
	if err := os.Remove(filepath.Join(root, "b", "two.mp4")); err != nil {
		t.Fatal(err)
	}
	r := mustScan(t, st, Options{Root: root, StartRel: "a"})
	if r.Missing != 0 {
		t.Errorf("scoped scan must not tombstone out-of-scope, got missing=%d", r.Missing)
	}
	items := loadByRel(t, st, r.RootID)
	if items["b/two.mp4"].Status != index.StatusActive {
		t.Error("out-of-scope item wrongly tombstoned")
	}
}

func TestScanPreflightDeadRoot(t *testing.T) {
	st := newStore(t)
	_, err := Scan(context.Background(), st, Options{Root: filepath.Join(t.TempDir(), "does-not-exist")})
	var sre *ScanRootError
	if !errors.As(err, &sre) {
		t.Fatalf("expected ScanRootError, got %v", err)
	}
}

func TestScanEnabledCategoriesGating(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"movie.mp4", "song.flac", "movie.nfo"})
	st := newStore(t)
	// Only the video CATEGORY enabled (W8-E taxonomy gate; nil Taxonomy => seed
	// classifier): song.flac (audio) excluded, but movie.nfo (sidecar) still
	// ingested. .mp4 -> video, .flac -> audio in the seed taxonomy.
	r := mustScan(t, st, Options{Root: root, EnabledCategories: []string{"video"}})
	items := loadByRel(t, st, r.RootID)
	if _, ok := items["movie.mp4"]; !ok {
		t.Error("video should be ingested")
	}
	if _, ok := items["song.flac"]; ok {
		t.Error("audio should be gated out when only video enabled")
	}
	if _, ok := items["movie.nfo"]; !ok {
		t.Error("sidecar must bypass the enabled-categories gate")
	}
	if it := items["movie.mp4"]; it.FileCategory != "video" || it.FileGroup != "video" {
		t.Errorf("movie.mp4 should classify as (video, video), got (%q, %q)", it.FileCategory, it.FileGroup)
	}
}

func TestScanEnabledGroupsGating(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"movie.mp4", "song.flac"})
	st := newStore(t)
	// Enabling a GROUP (audio-lossless) admits .flac while excluding .mp4 (video),
	// exercising the OR-of-category-or-group inclusion rule.
	r := mustScan(t, st, Options{Root: root, EnabledGroups: []string{"audio-lossless"}})
	items := loadByRel(t, st, r.RootID)
	if _, ok := items["song.flac"]; !ok {
		t.Error("audio-lossless group should be ingested")
	}
	if _, ok := items["movie.mp4"]; ok {
		t.Error("video should be gated out when only the audio-lossless group is enabled")
	}
}

func TestScanSidecarAssociation(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{
		"Movies/Film/Film.mp4",
		"Movies/Film/Film.nfo",   // per-stem nfo -> Film.mp4
		"Movies/Film/poster.jpg", // directory artwork -> primary (Film.mp4)
	})
	st := newStore(t)
	r := mustScan(t, st, Options{Root: root, EnabledPresets: []string{"-hidden_dotfiles"}})
	if r.Sidecars.Sidecars != 2 || r.Sidecars.Linked != 2 {
		t.Fatalf("expected 2 sidecars both linked: %+v", r.Sidecars)
	}
	items := loadByRel(t, st, r.RootID)
	filmID := items["Movies/Film/Film.mp4"].ID
	if items["Movies/Film/Film.nfo"].SidecarOf != filmID {
		t.Error("Film.nfo should link to Film.mp4")
	}
	if items["Movies/Film/poster.jpg"].SidecarOf != filmID {
		t.Error("poster.jpg should link to the directory primary Film.mp4")
	}
	// Sidecars are not hashed (mirrors central).
	if items["Movies/Film/Film.nfo"].QuickHash != "" {
		t.Error("sidecar must not be hashed")
	}
}

func TestScanSearchOffline(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"Movies/Arcane/Arcane.S01E01.mkv", "Music/song.flac", "Movies/Arcane/poster.jpg"})
	st := newStore(t)
	mustScan(t, st, Options{Root: root, EnabledPresets: []string{"-hidden_dotfiles"}})

	// Offline FTS trigram search over the local index.
	hits, err := st.Search(context.Background(), "arcane", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) == 0 {
		t.Fatal("expected a trigram match for 'arcane'")
	}
	// Default search excludes sidecars (poster.jpg).
	for _, h := range hits {
		if h.IsSidecar {
			t.Error("default search must exclude sidecars")
		}
	}
}
