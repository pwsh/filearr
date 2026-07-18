package history

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

// openTest opens a history store on a fresh temp file with a controllable clock.
func openTest(t *testing.T, now *time.Time) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "history.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	s.now = func() time.Time { return *now }
	t.Cleanup(func() { s.Close() })
	return s
}

func TestNormalize(t *testing.T) {
	cases := map[string]string{
		"  kind:video   size:>1G ": "kind:video size:>1G",
		"ext:pdf":                  "ext:pdf",
		"\tfoo\nbar":               "foo bar",
		"   ":                      "",
		"Arcane":                   "Arcane", // case preserved
	}
	for in, want := range cases {
		if got := Normalize(in); got != want {
			t.Errorf("Normalize(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestRecordRanksRepeatedHigher is the core P7-T6 accept: repeated queries rank
// higher over time.
func TestRecordRanksRepeatedHigher(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	ctx := context.Background()

	// "alpha" run 3×, "beta" once, "gamma" twice — same recency bucket, so pure
	// frequency ordering: alpha > gamma > beta.
	for i := 0; i < 3; i++ {
		mustRecord(t, s, ctx, "alpha")
	}
	mustRecord(t, s, ctx, "beta")
	mustRecord(t, s, ctx, "gamma")
	mustRecord(t, s, ctx, "gamma")

	top, err := s.Top(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(top) != 3 {
		t.Fatalf("want 3 entries, got %d", len(top))
	}
	if top[0].Query != "alpha" || top[1].Query != "gamma" || top[2].Query != "beta" {
		t.Fatalf("frecency order wrong: %+v", top)
	}
	if top[0].Hits != 3 {
		t.Errorf("alpha hits = %v, want 3", top[0].Hits)
	}
}

// TestRecencyBeatsFrequency: a fresh single query outranks a stale frequent one,
// because the recency multiplier (4× within the hour vs 0.25× when older than a
// week) dominates.
func TestRecencyBeatsFrequency(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	ctx := context.Background()

	// "stale" recorded 5× two weeks ago.
	now = now.Add(-14 * 24 * time.Hour)
	for i := 0; i < 5; i++ {
		mustRecord(t, s, ctx, "stale")
	}
	// "fresh" recorded once, just now.
	now = time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	mustRecord(t, s, ctx, "fresh")

	top, err := s.Top(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	// stale: 5 * 0.25 = 1.25 ; fresh: 1 * 4 = 4.0 → fresh wins.
	if top[0].Query != "fresh" {
		t.Fatalf("expected fresh (recent) to outrank stale (frequent): %+v", top)
	}
}

// TestNormalizedDedup: whitespace-variant queries collapse to one entry.
func TestNormalizedDedup(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	ctx := context.Background()
	mustRecord(t, s, ctx, "kind:video  size:>1G")
	mustRecord(t, s, ctx, "  kind:video size:>1G ")
	top, _ := s.Top(ctx, 10)
	if len(top) != 1 {
		t.Fatalf("whitespace variants should dedup to 1 entry, got %d: %+v", len(top), top)
	}
	if top[0].Hits != 2 {
		t.Errorf("hits = %v, want 2", top[0].Hits)
	}
}

// TestRecordBlankIgnored: a blank/whitespace query is not recorded.
func TestRecordBlankIgnored(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	ctx := context.Background()
	mustRecord(t, s, ctx, "   ")
	mustRecord(t, s, ctx, "")
	top, _ := s.Top(ctx, 10)
	if len(top) != 0 {
		t.Fatalf("blank queries must not be recorded, got %+v", top)
	}
}

// TestRetentionPrune: an entry untouched past the retention window is dropped on
// the next record.
func TestRetentionPrune(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	ctx := context.Background()

	now = now.Add(-100 * 24 * time.Hour) // 100 days ago (> 90d retention)
	mustRecord(t, s, ctx, "ancient")
	now = time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC) // back to present
	mustRecord(t, s, ctx, "current")

	top, _ := s.Top(ctx, 10)
	if len(top) != 1 || top[0].Query != "current" {
		t.Fatalf("expected only 'current' after retention prune, got %+v", top)
	}
}

// TestDecayAndFloorPrune forces the decay ceiling low so a decay pass runs, then
// asserts ranks were halved and sub-epsilon rows pruned.
func TestDecayAndFloorPrune(t *testing.T) {
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	s := openTest(t, &now)
	s.maxTotal = 3.0 // tiny ceiling so decay triggers quickly
	ctx := context.Background()

	// "hot" reaches rank 4 (survives a halving to 2.0 ≥ epsilon 1.0).
	for i := 0; i < 4; i++ {
		mustRecord(t, s, ctx, "hot")
	}
	// At this point total rank = 4 > 3, so the last record already decayed: after
	// the 4th upsert rank=4, sum=4>3 → halve to 2.0. A single "cold" entry (rank 1)
	// recorded now pushes sum to 3.0 (not > 3), no further decay.
	mustRecord(t, s, ctx, "cold")

	top, _ := s.Top(ctx, 10)
	// Find hot + cold.
	byq := map[string]float64{}
	for _, e := range top {
		byq[e.Query] = e.Hits
	}
	if byq["hot"] >= 4 {
		t.Errorf("expected 'hot' rank to have decayed below 4, got %v", byq["hot"])
	}
	if _, ok := byq["hot"]; !ok {
		t.Error("'hot' should survive decay (rank stayed >= epsilon)")
	}

	// Now drive many more decays so a rank-1 entry falls below the epsilon floor and
	// is pruned. Record "hot" repeatedly (keeps sum over ceiling) while "cold" is
	// never touched again — it should decay out.
	for i := 0; i < 20; i++ {
		mustRecord(t, s, ctx, "hot")
	}
	top, _ = s.Top(ctx, 10)
	for _, e := range top {
		if e.Query == "cold" {
			t.Errorf("'cold' should have been floor-pruned after repeated decays, got rank %v", e.Hits)
		}
	}
}

func mustRecord(t *testing.T, s *Store, ctx context.Context, q string) {
	t.Helper()
	if err := s.Record(ctx, q); err != nil {
		t.Fatalf("Record(%q): %v", q, err)
	}
}
