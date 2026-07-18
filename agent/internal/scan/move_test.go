package scan

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
)

func item(id, rel, quick string, size int64, content string) *index.Item {
	return &index.Item{ID: id, RelPath: rel, QuickHash: quick, Size: size, ContentHash: content, Status: index.StatusActive}
}

func TestPlanMovesUnambiguousRename(t *testing.T) {
	cands := []*index.Item{item("c1", "old.mp4", "q1", 100, "")}
	news := []*index.Item{item("n1", "new.mp4", "q1", 100, "")}
	plans, ambiguous := planMoves(cands, news)
	if len(plans) != 1 || ambiguous != 0 {
		t.Fatalf("expected 1 plan, 0 ambiguous; got %d/%d", len(plans), ambiguous)
	}
	if plans[0].survivor.ID != "c1" || plans[0].duplicate.ID != "n1" {
		t.Error("survivor should be the original candidate, duplicate the new row")
	}
}

func TestPlanMovesContentHashVeto(t *testing.T) {
	// Same quick_hash+size but different content_hash: a coincidental collision.
	// The full-hash mismatch must VETO the transfer -> ambiguous, no plan.
	cands := []*index.Item{item("c1", "old.mp4", "q1", 100, "cA")}
	news := []*index.Item{item("n1", "new.mp4", "q1", 100, "cB")}
	plans, ambiguous := planMoves(cands, news)
	if len(plans) != 0 || ambiguous != 1 {
		t.Fatalf("content veto: expected 0 plans, 1 ambiguous; got %d/%d", len(plans), ambiguous)
	}
}

func TestPlanMovesMultiWayAmbiguousRefusal(t *testing.T) {
	// Two candidates + two new rows all sharing (quick,size) and identical
	// content_hash: nothing can be pinned uniquely -> all ambiguous, no transfer.
	cands := []*index.Item{item("c1", "o1.bin", "q", 10, "same"), item("c2", "o2.bin", "q", 10, "same")}
	news := []*index.Item{item("n1", "p1.bin", "q", 10, "same"), item("n2", "p2.bin", "q", 10, "same")}
	plans, ambiguous := planMoves(cands, news)
	if len(plans) != 0 {
		t.Errorf("multi-way indistinguishable bucket must transfer nothing, got %d plans", len(plans))
	}
	if ambiguous == 0 {
		t.Error("expected ambiguous count > 0")
	}
}

func TestPlanMovesMultiWayRescuedByUniqueContent(t *testing.T) {
	// Same (quick,size) bucket but content_hash pins each pair uniquely.
	cands := []*index.Item{item("c1", "o1.bin", "q", 10, "A"), item("c2", "o2.bin", "q", 10, "B")}
	news := []*index.Item{item("n1", "p1.bin", "q", 10, "B"), item("n2", "p2.bin", "q", 10, "A")}
	plans, ambiguous := planMoves(cands, news)
	if len(plans) != 2 || ambiguous != 0 {
		t.Fatalf("unique content pins should rescue both: got %d plans, %d ambiguous", len(plans), ambiguous)
	}
}

func TestScanRenamePreservesIdentity(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "Film.mp4"), []byte("the film bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	st := newStore(t)
	r1 := mustScan(t, st, Options{Root: root})
	origID := loadByRel(t, st, r1.RootID)["Film.mp4"].ID

	// Rename (content identical -> same quick/content hash + size).
	if err := os.Rename(filepath.Join(root, "Film.mp4"), filepath.Join(root, "Film2.mp4")); err != nil {
		t.Fatal(err)
	}
	r2 := mustScan(t, st, Options{Root: root})
	if r2.Moved != 1 {
		t.Fatalf("expected moved=1, got %+v", r2)
	}
	items := loadByRel(t, st, r1.RootID)
	if _, gone := items["Film.mp4"]; gone {
		t.Error("old rel_path should no longer exist")
	}
	survivor, ok := items["Film2.mp4"]
	if !ok {
		t.Fatal("renamed file should exist at new rel_path")
	}
	if survivor.ID != origID {
		t.Errorf("rename must preserve identity: id %s -> %s", origID, survivor.ID)
	}
	if survivor.Status != index.StatusActive {
		t.Error("survivor should be active")
	}
}

// TestScanMultipleRenamesPreserveIdentities relocates two files to fresh names
// in one scan. Both old paths fully vacate (vanish) and both new paths appear, so
// move detection produces two plans and exercises the three-phase park-at-
// sentinel transfer loop, preserving each identity. (A pure name *swap* is NOT a
// move-detection scenario: both paths remain present, so the diff records two
// in-place content changes — identical to central's behaviour, which shares the
// same changed-branch. Hence the multi-plan case is tested via fresh targets.)
func TestScanMultipleRenamesPreserveIdentities(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "A.mp4"), []byte("aaaa content A"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "B.mp4"), []byte("bbbbbbbb content B longer"), 0o644); err != nil {
		t.Fatal(err)
	}
	st := newStore(t)
	r1 := mustScan(t, st, Options{Root: root})
	items := loadByRel(t, st, r1.RootID)
	idA, idB := items["A.mp4"].ID, items["B.mp4"].ID

	must(t, os.Rename(filepath.Join(root, "A.mp4"), filepath.Join(root, "A2.mp4")))
	must(t, os.Rename(filepath.Join(root, "B.mp4"), filepath.Join(root, "B2.mp4")))

	r2 := mustScan(t, st, Options{Root: root})
	if r2.Moved != 2 {
		t.Fatalf("two relocations should be moved=2, got %+v", r2)
	}
	if r2.Missing != 0 {
		t.Errorf("relocated files must not be tombstoned, got missing=%d", r2.Missing)
	}
	items = loadByRel(t, st, r1.RootID)
	if items["A2.mp4"].ID != idA {
		t.Errorf("A's identity should follow it to A2.mp4: want %s got %s", idA, items["A2.mp4"].ID)
	}
	if items["B2.mp4"].ID != idB {
		t.Errorf("B's identity should follow it to B2.mp4: want %s got %s", idB, items["B2.mp4"].ID)
	}
	if _, gone := items["A.mp4"]; gone {
		t.Error("old path A.mp4 should be gone")
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
