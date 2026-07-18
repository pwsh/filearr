package index

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func mustPending(t *testing.T, st *Store, want bool) {
	t.Helper()
	got, err := st.RebuiltPending(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("RebuiltPending = %v, want %v", got, want)
	}
}

// TestMarkerWrittenOnFreshCreate: opening a brand-new database sets the durable
// rebuilt marker (a fresh local seq base central must learn about), even though
// the in-memory Rebuilt flag stays false.
func TestMarkerWrittenOnFreshCreate(t *testing.T) {
	st, err := Open(filepath.Join(t.TempDir(), "fresh.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if st.Rebuilt {
		t.Error("fresh create must not set the in-memory Rebuilt flag")
	}
	mustPending(t, st, true)
}

// TestMarkerWrittenOnCorruptionRebuild: a corruption rebuild sets BOTH the
// in-memory Rebuilt flag and the durable marker.
func TestMarkerWrittenOnCorruptionRebuild(t *testing.T) {
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	st.Close()
	for _, s := range []string{"-wal", "-shm"} {
		_ = os.Remove(path + s)
	}
	if err := os.WriteFile(path, []byte("garbage not a sqlite db"), 0o644); err != nil {
		t.Fatal(err)
	}
	st2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	if !st2.Rebuilt {
		t.Error("corruption rebuild must set Rebuilt")
	}
	mustPending(t, st2, true)
}

// TestMarkerSurvivesProcessBoundary is the core regression: the E2E gap was that
// `scan` rebuilds (Rebuilt=true, in-memory) but a SEPARATE `reconcile` process
// reopens a now-clean file (Rebuilt=false) and lost the signal. The durable marker
// must survive close+reopen so the second process still sends rebuilt=true.
func TestMarkerSurvivesProcessBoundary(t *testing.T) {
	path := filepath.Join(t.TempDir(), "index.db")

	// "scan" process: fresh-create (or rebuild) writes the marker.
	st1, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	mustPending(t, st1, true)
	st1.Close()

	// "reconcile" process: reopen the clean, existing file — Rebuilt is false, but
	// the marker persists.
	st2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if st2.Rebuilt {
		t.Error("reopening a clean file must not set Rebuilt")
	}
	mustPending(t, st2, true)

	// After the reconcile clears it, a further reopen stays clear (idempotent).
	if err := st2.ClearRebuiltPending(context.Background()); err != nil {
		t.Fatal(err)
	}
	mustPending(t, st2, false)
	st2.Close()

	st3, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st3.Close()
	mustPending(t, st3, false)
}
