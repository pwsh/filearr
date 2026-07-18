package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCacheMissingFileIsNotAnError(t *testing.T) {
	_, ok, err := NewETagCache(t.TempDir()).Load()
	if err != nil {
		t.Fatalf("missing cache must not error, got %v", err)
	}
	if ok {
		t.Error("missing cache must report ok=false")
	}
}

func TestCacheSaveLoadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	cache := NewETagCache(dir)
	now := time.Now().UTC().Truncate(time.Second)
	doc := PolicyDoc{
		ETag: `"library/11"`, Scope: "library", Version: 11, AppliedVersion: 10,
		FetchedAt: now, Policy: []byte(`{"watch_mode":false}`),
	}
	if err := cache.Save(doc); err != nil {
		t.Fatal(err)
	}
	got, ok, err := cache.Load()
	if err != nil || !ok {
		t.Fatalf("load: ok=%v err=%v", ok, err)
	}
	if got.ETag != doc.ETag || got.Version != 11 || got.AppliedVersion != 10 || got.Scope != "library" {
		t.Errorf("round-trip metadata mismatch: %+v", got)
	}
	if !got.FetchedAt.Equal(now) {
		t.Errorf("fetched_at mismatch: %s != %s", got.FetchedAt, now)
	}
	pol, _ := got.Parsed()
	if pol.WatchMode == nil || *pol.WatchMode {
		t.Error("watch_mode:false must survive round-trip")
	}
}

func TestCacheAtomicWriteLeavesNoTemp(t *testing.T) {
	dir := t.TempDir()
	cache := NewETagCache(dir)
	if err := cache.Save(PolicyDoc{Scope: "none", Version: 0, Policy: []byte(`{}`)}); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name() != policyFileName {
			t.Errorf("unexpected leftover file %q (temp not cleaned?)", e.Name())
		}
	}
	if _, err := os.Stat(filepath.Join(dir, policyFileName)); err != nil {
		t.Errorf("policy.json not written: %v", err)
	}
}

func TestCacheEmptyPolicyNormalized(t *testing.T) {
	dir := t.TempDir()
	cache := NewETagCache(dir)
	if err := cache.Save(PolicyDoc{Scope: "none"}); err != nil { // nil Policy
		t.Fatal(err)
	}
	got, _, err := cache.Load()
	if err != nil {
		t.Fatal(err)
	}
	if string(got.Policy) != "{}" {
		t.Errorf("empty policy body must normalize to {}, got %s", got.Policy)
	}
}
