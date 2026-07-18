package thumbs

import (
	"context"
	"strings"
	"sync"
	"testing"
)

// fakeStore is an in-memory thumbs.Store for the pass tests.
type fakeStore struct {
	mu      sync.Mutex
	cands   []Candidate
	markers map[string]map[int]string // itemID -> tier -> key
}

func newFakeStore(cands ...Candidate) *fakeStore {
	return &fakeStore{cands: cands, markers: map[string]map[int]string{}}
}

func (s *fakeStore) ThumbCandidates(context.Context) ([]Candidate, error) {
	return s.cands, nil
}

func (s *fakeStore) ThumbMarkers(_ context.Context, itemID string) ([]Marker, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Marker
	for tier, key := range s.markers[itemID] {
		out = append(out, Marker{Tier: tier, CacheKey: key})
	}
	return out, nil
}

func (s *fakeStore) MarkThumb(_ context.Context, itemID string, tier int, key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.markers[itemID] == nil {
		s.markers[itemID] = map[int]string{}
	}
	s.markers[itemID][tier] = key
	return nil
}

// fakeUploader records uploads and returns a configurable "stored" verdict.
type fakeUploader struct {
	mu     sync.Mutex
	calls  []uploadCall
	stored bool
}

type uploadCall struct {
	LibraryRef string
	RelPath    string
	Tier       int
	Key        string
}

func (u *fakeUploader) Upload(_ context.Context, libraryRef, relPath string, tier int, key string, _ *ThumbBytes) (bool, error) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.calls = append(u.calls, uploadCall{libraryRef, relPath, tier, key})
	return u.stored, nil
}

func (u *fakeUploader) count() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return len(u.calls)
}

func imageCandidate(t *testing.T, dir, name, hash string) Candidate {
	t.Helper()
	writePNGAt(t, dir, name, 200, 120)
	return Candidate{
		ItemID:      "item-" + name,
		RootPath:    dir,
		RelPath:     name,
		MediaType:   mediaImage,
		ContentHash: hash,
	}
}

func TestRunPass_GeneratesUploadsAndIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	store := newFakeStore(
		imageCandidate(t, dir, "a.png", "hash-a"),
		imageCandidate(t, dir, "b.png", "hash-b"),
	)
	up := &fakeUploader{stored: true}
	tn := New(Config{Store: store, Uploader: up, RatePerSec: 1000})

	stats, err := tn.RunPass(context.Background())
	if err != nil {
		t.Fatalf("RunPass: %v", err)
	}
	// 2 items × {grid, preview} = 4 uploads.
	if up.count() != 4 {
		t.Fatalf("expected 4 uploads, got %d", up.count())
	}
	if stats.Generated != 4 {
		t.Fatalf("stats.Generated = %d, want 4", stats.Generated)
	}
	// Each upload used the content-addressed key for its (hash, tier).
	for _, c := range up.calls {
		hash := "hash-a"
		if strings.Contains(c.RelPath, "b.png") {
			hash = "hash-b"
		}
		if c.Key != CacheKey(hash, GeneratorVersion, c.Tier) {
			t.Fatalf("upload key %s != expected for %s tier %d", c.Key, c.RelPath, c.Tier)
		}
	}

	// Second pass: every marker matches the expected key -> zero new uploads.
	if _, err := tn.RunPass(context.Background()); err != nil {
		t.Fatalf("second RunPass: %v", err)
	}
	if up.count() != 4 {
		t.Fatalf("idempotency broken: expected still 4 uploads, got %d", up.count())
	}
}

func TestRunPass_DeferredNotMarkedRetries(t *testing.T) {
	dir := t.TempDir()
	store := newFakeStore(imageCandidate(t, dir, "a.png", "hash-a"))
	up := &fakeUploader{stored: false} // central declines (e.g. not yet replicated)
	tn := New(Config{Store: store, Uploader: up, RatePerSec: 1000})

	stats, _ := tn.RunPass(context.Background())
	if stats.Deferred != 2 || stats.Generated != 0 {
		t.Fatalf("expected 2 deferred, 0 generated, got %+v", stats)
	}
	// No markers were recorded, so a second pass retries the same 2 tiers.
	if _, err := tn.RunPass(context.Background()); err != nil {
		t.Fatal(err)
	}
	if up.count() != 4 {
		t.Fatalf("a deferred upload must retry next pass; got %d total uploads, want 4", up.count())
	}
}

func TestRunPass_VideoSkippedWithoutFFmpeg(t *testing.T) {
	dir := t.TempDir()
	store := newFakeStore(Candidate{
		ItemID: "v1", RootPath: dir, RelPath: "movie.mkv",
		MediaType: mediaVideo, ContentHash: "vh",
	})
	up := &fakeUploader{stored: true}
	// ffmpegPath defaults to "" -> video generation is skipped, never an error.
	tn := New(Config{Store: store, Uploader: up, RatePerSec: 1000})
	stats, err := tn.RunPass(context.Background())
	if err != nil {
		t.Fatalf("RunPass: %v", err)
	}
	if up.count() != 0 {
		t.Fatalf("video without ffmpeg must upload nothing, got %d", up.count())
	}
	if stats.Skipped != 2 {
		t.Fatalf("expected 2 skipped tiers, got %+v", stats)
	}
}

func TestRunPass_SidecarArtworkFirst(t *testing.T) {
	dir := t.TempDir()
	// A video with NO ffmpeg: the ONLY route to a thumbnail is the artwork sidecar
	// (central's Rule 0). A real poster image is present; IsArtwork claims it.
	writePNGAt(t, dir, "poster.jpg", 300, 200)
	store := newFakeStore(Candidate{
		ItemID: "v1", RootPath: dir, RelPath: "movie.mkv",
		MediaType: mediaVideo, ContentHash: "vh",
		SidecarRels: []string{"poster.jpg"},
	})
	up := &fakeUploader{stored: true}
	tn := New(Config{
		Store: store, Uploader: up, RatePerSec: 1000,
		IsArtwork: func(rel string) bool { return rel == "poster.jpg" },
	})
	stats, err := tn.RunPass(context.Background())
	if err != nil {
		t.Fatalf("RunPass: %v", err)
	}
	if up.count() != 2 || stats.Generated != 2 {
		t.Fatalf("sidecar artwork should yield 2 thumbnails for a video with no ffmpeg; got uploads=%d stats=%+v", up.count(), stats)
	}
}

func TestRunPass_UnhashedSkipped(t *testing.T) {
	dir := t.TempDir()
	c := imageCandidate(t, dir, "a.png", "")
	c.QuickHash = "" // neither hash -> no addressable key
	store := newFakeStore(c)
	up := &fakeUploader{stored: true}
	tn := New(Config{Store: store, Uploader: up, RatePerSec: 1000})
	if _, err := tn.RunPass(context.Background()); err != nil {
		t.Fatal(err)
	}
	if up.count() != 0 {
		t.Fatalf("an unhashed item has no key and must upload nothing, got %d", up.count())
	}
}
