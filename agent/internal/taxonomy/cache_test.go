package taxonomy

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// buildPayload renders a compact wire payload at a given version for a stub
// central taxonomy endpoint.
func buildPayload(t *testing.T, version int) []byte {
	t.Helper()
	ext := map[string]string{"mkv": "video", "flac": "audio-lossless"}
	g2c := map[string]string{"video": "video", "audio-lossless": "audio"}
	vid, aud := "video", "audio"
	tax := New(version, ext, g2c, map[string]*string{"video": &vid, "audio": &aud}, []string{"video", "audio"})
	buf, err := tax.Marshal()
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return buf
}

func TestNewCacheSeedFallback(t *testing.T) {
	// An empty data dir (never contacted) => the baked-in seed snapshot.
	c := NewCache(t.TempDir(), nil)
	if c.Version() != SeedVersion {
		t.Errorf("fresh cache version = %d, want seed %d", c.Version(), SeedVersion)
	}
	if cat, _ := c.Current().Classify("a.mkv"); cat != "video" {
		t.Errorf("seed cache should classify a.mkv as video, got %q", cat)
	}
}

func TestCachePersistAndReload(t *testing.T) {
	dir := t.TempDir()
	// Persist a v4 snapshot, then reload a fresh cache from disk.
	if err := os.WriteFile(filepath.Join(dir, cacheFileName), buildPayload(t, 4), 0o644); err != nil {
		t.Fatal(err)
	}
	c := NewCache(dir, nil)
	if c.Version() != 4 {
		t.Errorf("reloaded version = %d, want 4", c.Version())
	}
}

func TestCacheCorruptFallsBackToSeed(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, cacheFileName), []byte("{corrupt"), 0o644); err != nil {
		t.Fatal(err)
	}
	c := NewCache(dir, nil)
	if c.Version() != SeedVersion {
		t.Errorf("corrupt cache should fall back to seed, got version %d", c.Version())
	}
}

func TestRefreshVersionGated(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(buildPayload(t, 5))
	}))
	defer srv.Close()

	dir := t.TempDir()
	c := NewCache(dir, nil) // seed, version 0
	client := NewClient(ClientConfig{BaseURL: srv.URL, AgentID: "aid"})

	// wantVersion not newer => no fetch, no-op.
	if err := c.Refresh(context.Background(), client, 0); err != nil {
		t.Fatalf("gated refresh: %v", err)
	}
	if hits != 0 {
		t.Fatalf("expected no fetch for non-newer version, got %d hits", hits)
	}

	// wantVersion newer => fetch + apply + persist.
	if err := c.Refresh(context.Background(), client, 5); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if hits != 1 {
		t.Fatalf("expected 1 fetch, got %d", hits)
	}
	if c.Version() != 5 {
		t.Errorf("after refresh version = %d, want 5", c.Version())
	}
	// The fetched taxonomy is now persisted: a fresh cache reads v5 from disk.
	if reloaded := NewCache(dir, nil); reloaded.Version() != 5 {
		t.Errorf("persisted version = %d, want 5", reloaded.Version())
	}

	// A subsequent gate at the same version does not re-fetch.
	if err := c.Refresh(context.Background(), client, 5); err != nil {
		t.Fatalf("re-refresh: %v", err)
	}
	if hits != 1 {
		t.Fatalf("expected no re-fetch at same version, got %d hits", hits)
	}
}

func TestRefreshFetchErrorKeepsCurrent(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"boom"}`, http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := NewCache(t.TempDir(), nil)
	client := NewClient(ClientConfig{BaseURL: srv.URL, AgentID: "aid"})
	if err := c.Refresh(context.Background(), client, 9); err == nil {
		t.Error("expected error on 500")
	}
	if c.Version() != SeedVersion {
		t.Errorf("failed refresh must keep the current snapshot, got version %d", c.Version())
	}
}

func TestClientFetchSendsBearer(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer tok" {
			t.Errorf("Authorization = %q, want Bearer tok", got)
		}
		if r.URL.Path != "/api/v1/agents/aid/taxonomy" {
			t.Errorf("path = %q", r.URL.Path)
		}
		_, _ = w.Write(buildPayload(t, 2))
	}))
	defer srv.Close()

	client := NewClient(ClientConfig{
		BaseURL: srv.URL, AgentID: "aid",
		AuthFn: func() string { return "tok" },
	})
	snap, err := client.Fetch(context.Background())
	if err != nil {
		t.Fatalf("fetch: %v", err)
	}
	if snap.Version() != 2 {
		t.Errorf("fetched version = %d, want 2", snap.Version())
	}
}
