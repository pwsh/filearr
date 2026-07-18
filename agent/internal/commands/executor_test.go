package commands

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/scan"
)

// fakeRoots is an in-memory RootLister so the executor needs no SQLite store.
type fakeRoots struct{ paths []string }

func (f fakeRoots) Roots(_ context.Context) ([]index.RootRef, error) {
	out := make([]index.RootRef, len(f.paths))
	for i, p := range f.paths {
		out[i] = index.RootRef{ID: "root", Path: p}
	}
	return out, nil
}

func writeFile(t *testing.T, dir, rel string, data []byte) string {
	t.Helper()
	full := filepath.Join(dir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return full
}

func TestExecuteStatExisting(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a/x.mkv", []byte("hello world"))
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)

	res, err := ex.Execute(context.Background(), KindStatCheck, map[string]any{
		"library_ref": root, "rel_path": "a/x.mkv",
	})
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if !res.Exists || res.Size == nil || *res.Size != 11 {
		t.Fatalf("bad stat result: %+v", res)
	}
	if res.Mtime == nil || *res.Mtime <= 0 {
		t.Fatalf("missing mtime: %+v", res)
	}
	if res.QuickHash != nil || res.ContentHash != nil || res.ContentSkipped {
		t.Fatalf("stat must not hash: %+v", res)
	}
}

func TestExecuteStatMissing(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	res, err := ex.Execute(context.Background(), KindStatCheck, map[string]any{
		"library_ref": root, "rel_path": "gone.mkv",
	})
	if err != nil {
		t.Fatalf("missing must be exists=false, not error: %v", err)
	}
	if res.Exists || res.ContentSkipped {
		t.Fatalf("bad missing stat: %+v", res)
	}
}

func TestExecuteRehashHashesMatchScanHelpers(t *testing.T) {
	root := t.TempDir()
	data := []byte("the quick brown fox jumps over the lazy dog")
	full := writeFile(t, root, "b/y.bin", data)
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)

	res, err := ex.Execute(context.Background(), KindRehashCheck, map[string]any{
		"library_ref": root, "rel_path": "b/y.bin", "content": true,
	})
	if err != nil {
		t.Fatalf("rehash: %v", err)
	}
	wantQuick, _ := scan.QuickHash(full, int64(len(data)))
	wantContent, _ := scan.FullHash(full)
	if res.QuickHash == nil || *res.QuickHash != wantQuick {
		t.Fatalf("quick_hash mismatch: got %v want %s", res.QuickHash, wantQuick)
	}
	if res.ContentHash == nil || *res.ContentHash != wantContent {
		t.Fatalf("content_hash mismatch: got %v want %s", res.ContentHash, wantContent)
	}
	if res.ContentSkipped {
		t.Fatalf("content should not be skipped: %+v", res)
	}
}

func TestExecuteRehashOversizeSkipsContent(t *testing.T) {
	root := t.TempDir()
	data := []byte("0123456789abcdef")
	full := writeFile(t, root, "big.bin", data)
	ex := NewExecutor(fakeRoots{[]string{root}}, 1) // ceiling 1 byte => oversize

	res, err := ex.Execute(context.Background(), KindRehashCheck, map[string]any{
		"library_ref": root, "rel_path": "big.bin", "content": true,
	})
	if err != nil {
		t.Fatalf("rehash oversize: %v", err)
	}
	wantQuick, _ := scan.QuickHash(full, int64(len(data)))
	if res.QuickHash == nil || *res.QuickHash != wantQuick {
		t.Fatalf("quick_hash should always be present: %+v", res)
	}
	if res.ContentHash != nil || !res.ContentSkipped {
		t.Fatalf("oversize content must be skipped (null + flag): %+v", res)
	}
}

func TestExecuteRehashMissingFlagsContentSkipped(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	res, err := ex.Execute(context.Background(), KindRehashCheck, map[string]any{
		"library_ref": root, "rel_path": "nope.bin", "content": true,
	})
	if err != nil {
		t.Fatalf("missing rehash must be exists=false, not error: %v", err)
	}
	if res.Exists || !res.ContentSkipped {
		t.Fatalf("missing rehash: expected exists=false + content_skipped: %+v", res)
	}
}

func TestExecuteUnknownRootRefused(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	if _, err := ex.Execute(context.Background(), KindStatCheck, map[string]any{
		"library_ref": filepath.Join(root, "elsewhere"), "rel_path": "x",
	}); err == nil {
		t.Fatal("expected error for unknown library_ref")
	}
}

func TestExecuteTraversalRefused(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	if _, err := ex.Execute(context.Background(), KindStatCheck, map[string]any{
		"library_ref": root, "rel_path": "../../etc/passwd",
	}); err == nil {
		t.Fatal("expected error for a rel_path escaping the root")
	}
}
