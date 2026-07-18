package index

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/thumbs"
)

func insertThumbItem(t *testing.T, st *Store, rootID string, it *Item) *Item {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := NewID()
	it.ID = id
	it.RootID = rootID
	if it.Filename == "" {
		it.Filename = filepath.Base(it.RelPath)
	}
	if it.Status == "" {
		it.Status = StatusActive
	}
	it.FirstSeen = time.Now()
	it.LastSeen = time.Now()
	if err := InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return it
}

func TestThumbCandidates(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")

	img := insertThumbItem(t, st, rid, &Item{
		RelPath: "Photos/beach.jpg", MediaType: "image", ContentHash: "ch1", QuickHash: "q1",
	})
	// An artwork sidecar child of the image (links via sidecar_of).
	insertThumbItem(t, st, rid, &Item{
		RelPath: "Photos/beach-thumb.jpg", MediaType: "image", QuickHash: "q2",
		IsSidecar: true, SidecarOf: img.ID,
	})
	// A hashed video (thumbnailable).
	insertThumbItem(t, st, rid, &Item{
		RelPath: "Movies/film.mkv", MediaType: "video", QuickHash: "qv",
	})
	// Excluded: a document (not thumbnailable on the agent).
	insertThumbItem(t, st, rid, &Item{
		RelPath: "Docs/manual.pdf", MediaType: "document", QuickHash: "qd",
	})
	// Excluded: an unhashed image (no addressable key yet).
	insertThumbItem(t, st, rid, &Item{
		RelPath: "Photos/nohash.png", MediaType: "image",
	})

	cands, err := st.ThumbCandidates(ctx)
	if err != nil {
		t.Fatalf("ThumbCandidates: %v", err)
	}
	byRel := map[string]thumbs.Candidate{}
	for _, c := range cands {
		byRel[c.RelPath] = c
	}
	if len(cands) != 2 {
		t.Fatalf("expected 2 candidates (image + video), got %d: %+v", len(cands), byRel)
	}
	beach, ok := byRel["Photos/beach.jpg"]
	if !ok {
		t.Fatal("image candidate missing")
	}
	if beach.RootPath != "/media" || beach.ContentHash != "ch1" {
		t.Fatalf("candidate root/hash wrong: %+v", beach)
	}
	if len(beach.SidecarRels) != 1 || beach.SidecarRels[0] != "Photos/beach-thumb.jpg" {
		t.Fatalf("expected the linked artwork sidecar, got %+v", beach.SidecarRels)
	}
	if _, ok := byRel["Docs/manual.pdf"]; ok {
		t.Fatal("a document must not be a candidate")
	}
	if _, ok := byRel["Photos/nohash.png"]; ok {
		t.Fatal("an unhashed item must not be a candidate")
	}
}

func TestThumbMarkersRoundTrip(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")
	it := insertThumbItem(t, st, rid, &Item{
		RelPath: "a.png", MediaType: "image", ContentHash: "ch",
	})

	if m, _ := st.ThumbMarkers(ctx, it.ID); len(m) != 0 {
		t.Fatalf("expected no markers initially, got %d", len(m))
	}
	if err := st.MarkThumb(ctx, it.ID, thumbs.TierGrid, "key-grid"); err != nil {
		t.Fatal(err)
	}
	if err := st.MarkThumb(ctx, it.ID, thumbs.TierPreview, "key-preview"); err != nil {
		t.Fatal(err)
	}
	// Upsert: re-marking the same tier replaces the key, never duplicates.
	if err := st.MarkThumb(ctx, it.ID, thumbs.TierGrid, "key-grid-2"); err != nil {
		t.Fatal(err)
	}

	markers, err := st.ThumbMarkers(ctx, it.ID)
	if err != nil {
		t.Fatal(err)
	}
	got := map[int]string{}
	for _, m := range markers {
		got[m.Tier] = m.CacheKey
	}
	if len(got) != 2 || got[thumbs.TierGrid] != "key-grid-2" || got[thumbs.TierPreview] != "key-preview" {
		t.Fatalf("marker round-trip wrong: %+v", got)
	}
}

func TestThumbMarkerCascadeOnItemDelete(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	rid := rootID(t, st, "/media")
	it := insertThumbItem(t, st, rid, &Item{RelPath: "a.png", MediaType: "image", ContentHash: "ch"})
	if err := st.MarkThumb(ctx, it.ID, thumbs.TierGrid, "k"); err != nil {
		t.Fatal(err)
	}
	tx, _ := st.Begin(ctx)
	if err := DeleteItem(ctx, tx, it.ID); err != nil {
		t.Fatal(err)
	}
	tx.Commit()
	// The FK CASCADE drops the marker with its item.
	if m, _ := st.ThumbMarkers(ctx, it.ID); len(m) != 0 {
		t.Fatalf("marker should cascade-delete with its item, got %d", len(m))
	}
}
