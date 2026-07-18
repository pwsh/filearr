package index

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// TestCorruptIndexRebuilds writes valid data, corrupts the database file with
// garbage, then reopens: IntegrityGuard must delete and recreate it, flag
// Rebuilt=true, and come back clean and empty (disposable-index philosophy,
// invariant 1). A subsequent full scan repopulates it (proven elsewhere).
func TestCorruptIndexRebuilds(t *testing.T) {
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	rid := rootID(t, st, "/media")
	insert(t, st, rid, "before.mkv", false)
	st.Close()

	// Overwrite the file with garbage that is not a valid SQLite database. (The
	// -wal/-shm sidecars are removed first so the header bytes are authoritative.)
	for _, s := range []string{"-wal", "-shm"} {
		_ = os.Remove(path + s)
	}
	if err := os.WriteFile(path, []byte("this is not a sqlite database at all, just garbage bytes"), 0o644); err != nil {
		t.Fatal(err)
	}

	st2, err := Open(path)
	if err != nil {
		t.Fatalf("Open on a corrupt db should rebuild, not error: %v", err)
	}
	defer st2.Close()
	if !st2.Rebuilt {
		t.Fatal("corrupt index should be flagged Rebuilt=true")
	}

	// The rebuilt store is clean and empty.
	var result string
	if err := st2.DB().QueryRow(`PRAGMA integrity_check`).Scan(&result); err != nil || result != "ok" {
		t.Fatalf("rebuilt db should pass integrity_check, got %q err=%v", result, err)
	}
	rid2 := rootID(t, st2, "/media")
	items, err := st2.LoadItems(context.Background(), rid2)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 0 {
		t.Errorf("rebuilt index must start empty, got %d items", len(items))
	}
}

// TestFreshOpenNotFlaggedRebuilt: a brand-new database opens clean.
func TestFreshOpenNotFlaggedRebuilt(t *testing.T) {
	st, err := Open(filepath.Join(t.TempDir(), "fresh.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if st.Rebuilt {
		t.Error("a fresh database must not be flagged Rebuilt")
	}
}
