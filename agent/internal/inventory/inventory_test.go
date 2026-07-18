package inventory

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"

	"github.com/filearr/filearr/agent/internal/pathspec"
)

func lstat(t *testing.T, p string) os.FileInfo {
	t.Helper()
	fi, err := os.Lstat(p)
	if err != nil {
		t.Fatal(err)
	}
	return fi
}

func TestStatCollector(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "a.txt")
	if err := os.WriteFile(f, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	fields, err := statCollector{}.Collect(context.Background(), f, lstat(t, f))
	if err != nil {
		t.Fatal(err)
	}
	if fields["size"].(int64) != 5 {
		t.Fatalf("size: %v", fields["size"])
	}
	if _, ok := fields["mtime_ns"].(int64); !ok {
		t.Fatalf("mtime_ns missing/wrong type: %v", fields["mtime_ns"])
	}
	if _, ok := fields["mode"].(string); !ok {
		t.Fatalf("mode missing")
	}
	if fields["is_dir"].(bool) {
		t.Fatalf("is_dir should be false")
	}
}

func TestOwnerCollectorSmoke(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "o.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fields, err := ownerCollector{}.Collect(context.Background(), f, lstat(t, f))
	if err != nil {
		t.Fatalf("owner collect: %v", err)
	}
	switch runtime.GOOS {
	case "windows":
		if _, ok := fields["owner_sid"].(string); !ok {
			t.Fatalf("windows owner_sid missing: %v", fields)
		}
	default:
		if _, ok := fields["uid"]; !ok {
			t.Fatalf("posix uid missing: %v", fields)
		}
		if _, ok := fields["gid"]; !ok {
			t.Fatalf("posix gid missing: %v", fields)
		}
	}
}

func TestPermsCollectorSmoke(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "p.txt")
	if err := os.WriteFile(f, []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	fields, err := permsCollector{}.Collect(context.Background(), f, lstat(t, f))
	if err != nil {
		t.Fatalf("perms collect: %v", err)
	}
	switch runtime.GOOS {
	case "windows":
		_, hasCount := fields["ace_count"]
		_, hasDacl := fields["dacl"]
		if !hasCount && !hasDacl {
			t.Fatalf("windows perms: expected ace_count or dacl: %v", fields)
		}
	default:
		if fields["mode_octal"] != "0640" {
			t.Fatalf("posix mode_octal: %v", fields["mode_octal"])
		}
	}
}

func TestPlaceholderCollectorSmoke(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "ph.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fields, err := placeholderCollector{}.Collect(context.Background(), f, lstat(t, f))
	if err != nil {
		t.Fatal(err)
	}
	// A normal local file is never a placeholder on any OS.
	if fields["placeholder"].(bool) {
		t.Fatalf("normal file flagged as placeholder: %v", fields)
	}
	if isPlaceholder(lstat(t, f)) {
		t.Fatalf("isPlaceholder true for normal file")
	}
}

func TestRegistryFailSoftUnknownCollector(t *testing.T) {
	r := NewRunner(nil, pathspec.OSHost())
	resolved, unknown := r.resolveCollectors([]string{"stat", "bogus", "owner"})
	if len(resolved) != 2 {
		t.Fatalf("expected 2 resolved, got %v", collectorNames(resolved))
	}
	if !reflect.DeepEqual(unknown, []string{"bogus"}) {
		t.Fatalf("unknown: %v", unknown)
	}
	// Registration order is preserved (stat before owner).
	if resolved[0].Name() != "stat" || resolved[1].Name() != "owner" {
		t.Fatalf("order not preserved: %v", collectorNames(resolved))
	}
}

func TestCapabilitiesShape(t *testing.T) {
	caps := Capabilities()
	if caps["inventory_version"] != CapabilityVersion {
		t.Fatalf("version: %v", caps["inventory_version"])
	}
	names, ok := caps["inventory_collectors"].([]string)
	if !ok {
		t.Fatalf("collectors wrong type: %T", caps["inventory_collectors"])
	}
	want := []string{"owner", "perms", "placeholder", "stat"} // sorted
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("collectors: %v want %v", names, want)
	}
}

func TestEncodeResultInlineVsUpload(t *testing.T) {
	small := []map[string]any{{"path": "/a", "size": int64(1)}}
	res, err := encodeResult(Summary{}, small, InlineMaxBytes)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Inlineable() || res.Inline == nil {
		t.Fatalf("small result should inline")
	}

	// A tiny cap forces the upload path.
	res, err = encodeResult(Summary{}, small, 4)
	if err != nil {
		t.Fatal(err)
	}
	if res.Inlineable() || res.Blob == nil {
		t.Fatalf("over-cap result should be a blob")
	}
	if res.NDJSONBytes == 0 {
		t.Fatalf("NDJSONBytes should be recorded")
	}

	// Zero entries still inline with a non-nil empty slice.
	res, _ = encodeResult(Summary{}, nil, InlineMaxBytes)
	if !res.Inlineable() || res.Inline == nil || len(res.Inline) != 0 {
		t.Fatalf("empty result should inline as empty slice: %#v", res.Inline)
	}
}

func TestRunEndToEnd(t *testing.T) {
	root := t.TempDir()
	// Two real files + one excluded (.DS_Store via os_metadata bundle) + a pruned
	// node_modules dir.
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.WriteFile(filepath.Join(root, "doc.txt"), []byte("a"), 0o644))
	must(os.MkdirAll(filepath.Join(root, "sub"), 0o755))
	must(os.WriteFile(filepath.Join(root, "sub", "b.dat"), []byte("bb"), 0o644))
	must(os.WriteFile(filepath.Join(root, ".DS_Store"), []byte("junk"), 0o644))
	must(os.MkdirAll(filepath.Join(root, "node_modules", "pkg"), 0o755))
	must(os.WriteFile(filepath.Join(root, "node_modules", "pkg", "x.js"), []byte("x"), 0o644))

	r := NewRunner(nil, pathspec.OSHost())
	res, err := r.Run(context.Background(), Command{
		Preset:     pathspec.PresetCustom,
		Paths:      []string{root},
		Collectors: []string{"stat"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Summary.RootsExpanded != 1 {
		t.Fatalf("roots: %d", res.Summary.RootsExpanded)
	}
	// doc.txt + sub/b.dat survive; .DS_Store and node_modules pruned.
	if res.Summary.Entries != 2 {
		t.Fatalf("entries: %d (%v)", res.Summary.Entries, res.Inline)
	}
	rels := map[string]bool{}
	for _, e := range res.Inline {
		rels[e["rel"].(string)] = true
		if _, ok := e["size"]; !ok {
			t.Fatalf("stat fields missing in %v", e)
		}
	}
	if !rels["doc.txt"] || !rels["sub/b.dat"] {
		t.Fatalf("wrong rels: %v", rels)
	}
	if !reflect.DeepEqual(res.Summary.CollectorsRun, []string{"stat"}) {
		t.Fatalf("collectors_run: %v", res.Summary.CollectorsRun)
	}
}

func TestRunMaxDepthAndMaxEntries(t *testing.T) {
	root := t.TempDir()
	// root/f0.txt, root/d1/f1.txt, root/d1/d2/f2.txt
	os.WriteFile(filepath.Join(root, "f0.txt"), []byte("0"), 0o644)
	os.MkdirAll(filepath.Join(root, "d1", "d2"), 0o755)
	os.WriteFile(filepath.Join(root, "d1", "f1.txt"), []byte("1"), 0o644)
	os.WriteFile(filepath.Join(root, "d1", "d2", "f2.txt"), []byte("2"), 0o644)

	r := NewRunner(nil, pathspec.OSHost())
	// max_depth=1 => root files + one level down, not d2.
	res, err := r.Run(context.Background(), Command{
		Paths: []string{root}, Collectors: []string{"stat"}, MaxDepth: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Summary.Entries != 2 {
		t.Fatalf("max_depth=1 entries: %d", res.Summary.Entries)
	}

	// max_entries=1 => capped.
	res, err = r.Run(context.Background(), Command{
		Paths: []string{root}, Collectors: []string{"stat"}, MaxEntries: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Summary.Entries != 1 || !res.Summary.EntriesCapped {
		t.Fatalf("max_entries cap: entries=%d capped=%v", res.Summary.Entries, res.Summary.EntriesCapped)
	}
}

func TestRunNoKnownCollectorsErrors(t *testing.T) {
	root := t.TempDir()
	r := NewRunner(nil, pathspec.OSHost())
	_, err := r.Run(context.Background(), Command{Paths: []string{root}, Collectors: []string{"bogus"}})
	if err == nil {
		t.Fatalf("expected error when no collectors resolve")
	}
}

func TestRunBadRegexErrors(t *testing.T) {
	root := t.TempDir()
	r := NewRunner(nil, pathspec.OSHost())
	_, err := r.Run(context.Background(), Command{
		Paths: []string{root}, Collectors: []string{"stat"}, IncludeRegex: []string{"("},
	})
	if err == nil {
		t.Fatalf("expected bad-regex error")
	}
}
