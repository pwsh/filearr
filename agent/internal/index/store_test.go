package index

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func openTemp(t *testing.T) (*Store, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st, path
}

func insert(t *testing.T, st *Store, rootID, rel string, sidecar bool) *Item {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := NewID()
	it := &Item{
		ID: id, RootID: rootID, RelPath: rel, Filename: filepath.Base(rel),
		Extension: "mkv", Size: 10, MtimeNs: time.Now().UnixNano(),
		QuickHash: "q", FileCategory: "video", Status: StatusActive, IsSidecar: sidecar,
		FirstSeen: time.Now(), LastSeen: time.Now(),
	}
	if err := InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return it
}

func rootID(t *testing.T, st *Store, path string) string {
	t.Helper()
	ctx := context.Background()
	tx, _ := st.Begin(ctx)
	id, err := EnsureRoot(ctx, tx, path)
	if err != nil {
		t.Fatal(err)
	}
	tx.Commit()
	return id
}

func TestFTSTriggersKeepProjectionInSync(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")
	insert(t, st, rid, "Movies/Arcane.S01E01.mkv", false)

	hits, err := st.Search(ctx, "arcane", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 1 {
		t.Fatalf("insert trigger should index the row, got %d hits", len(hits))
	}

	// Update the rel_path -> old trigram must drop, new one match.
	it := hits[0]
	tx, _ := st.Begin(ctx)
	it.RelPath = "Shows/Renamed.mkv"
	it.Filename = "Renamed.mkv"
	if err := UpdateItem(ctx, tx, &it); err != nil {
		t.Fatal(err)
	}
	tx.Commit()

	if h, _ := st.Search(ctx, "arcane", false, 10); len(h) != 0 {
		t.Error("update trigger should drop the stale trigram entry")
	}
	if h, _ := st.Search(ctx, "renamed", false, 10); len(h) != 1 {
		t.Error("update trigger should index the new filename")
	}

	// Delete -> projection empty.
	tx, _ = st.Begin(ctx)
	if err := DeleteItem(ctx, tx, it.ID); err != nil {
		t.Fatal(err)
	}
	tx.Commit()
	if h, _ := st.Search(ctx, "renamed", false, 10); len(h) != 0 {
		t.Error("delete trigger should remove the row from the projection")
	}
}

func TestSearchExcludesSidecarsByDefault(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")
	insert(t, st, rid, "Movies/Film.mkv", false)
	insert(t, st, rid, "Movies/Film.nfo", true) // sidecar (also matches "film")

	if h, _ := st.Search(ctx, "film", false, 10); len(h) != 1 {
		t.Errorf("default search should exclude sidecars, got %d", len(h))
	}
	if h, _ := st.Search(ctx, "film", true, 10); len(h) != 2 {
		t.Errorf("explicit sidecar search should include both, got %d", len(h))
	}
}

func TestLocalSeqNoMonotonic(t *testing.T) {
	st, _ := openTemp(t)
	rid := rootID(t, st, "/media")
	a := insert(t, st, rid, "a.mkv", false)
	b := insert(t, st, rid, "b.mkv", false)
	if !(b.LocalSeqNo > a.LocalSeqNo) {
		t.Errorf("local_seq_no must be monotonic: a=%d b=%d", a.LocalSeqNo, b.LocalSeqNo)
	}
}

func TestNullRoundTrip(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")
	// A sidecar with empty hashes and no sidecar_of: empties must persist as NULL
	// and read back as "" (the self-heal signal relies on this).
	it := insert(t, st, rid, "poster.jpg", true)
	it.QuickHash = ""
	it.ContentHash = ""
	tx, _ := st.Begin(ctx)
	if err := UpdateItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	tx.Commit()

	got, err := st.LoadItems(ctx, rid)
	if err != nil {
		t.Fatal(err)
	}
	row := got["poster.jpg"]
	if row.QuickHash != "" || row.ContentHash != "" || row.SidecarOf != "" || row.SyncedAt != nil {
		t.Errorf("NULLs should round-trip to zero values: %+v", row)
	}
}

// TestReopenPreservesData ensures WAL data survives a close/reopen (offline
// durability).
func TestReopenPreservesData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	rid := rootID(t, st, "/media")
	insert(t, st, rid, "keep.mkv", false)
	st.Close()

	st2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	if st2.Rebuilt {
		t.Error("a clean reopen must not report a rebuild")
	}
	if h, _ := st2.Search(context.Background(), "keep", false, 10); len(h) != 1 {
		t.Error("data should survive a close/reopen")
	}
}
