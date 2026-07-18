package scan

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// mktree writes each rel path (posix, "/"-separated) as a 1-byte file under root.
func mktree(t *testing.T, root string, files []string) {
	t.Helper()
	for _, rel := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// walkRels walks root with a library spec and returns the sorted rel set.
func walkRels(t *testing.T, root string, presets, excludes, includes []string) []string {
	t.Helper()
	spec := BuildLibrarySpec(presets, excludes, includes)
	var got []string
	if err := Walk(root, "", spec, func(e WalkEntry) error {
		got = append(got, e.Rel)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	sort.Strings(got)
	return got
}

// TestWalkNodeModulesNeverDescended proves directory pruning: nothing under a
// populated node_modules/ surfaces (V05-class — prune wins over any descent).
func TestWalkNodeModulesNeverDescended(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{
		"project/src/index.js",
		"project/node_modules/pkg/deep/a.js",
		"project/node_modules/pkg/deep/b.js",
		"project/node_modules/.bin/tool",
		"Movies/film.mp4",
	})
	got := walkRels(t, root, []string{"node_modules_build"}, nil, nil)
	for _, r := range got {
		if contains(r, "node_modules") {
			t.Errorf("node_modules content leaked into walk: %q", r)
		}
	}
	if !hasStr(got, "project/src/index.js") || !hasStr(got, "Movies/film.mp4") {
		t.Errorf("expected real files present, got %v", got)
	}
}

// TestWalkDefaultDotfileSkip: hidden_dotfiles is default-on and reproduces the
// legacy unconditional dotfile skip (dot-dirs pruned, dotfiles dropped).
func TestWalkDefaultDotfileSkip(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{
		"keep.mp4", "sub/keep2.mkv", ".hidden_file",
		".config/settings.ini", "sub/.dotfile",
	})
	got := walkRels(t, root, nil, nil, nil)
	if !equalStrs(got, []string{"keep.mp4", "sub/keep2.mkv"}) {
		t.Errorf("default dotfile skip failed, got %v", got)
	}
}

func TestWalkDisableHiddenDotfilesSentinel(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"keep.mp4", ".hidden_file", "sub/.dotfile"})
	got := walkRels(t, root, []string{"-hidden_dotfiles"}, nil, nil)
	want := []string{".hidden_file", "keep.mp4", "sub/.dotfile"}
	if !equalStrs(got, want) {
		t.Errorf("expected surfaced dotfiles %v, got %v", want, got)
	}
}

// TestWalkR1SidecarKeptStrayDropped: an excluded file a sidecar classifier claims
// is kept (indexed parent); a stray excluded file is dropped; a sidecar under a
// pruned dir is gone (R1 directory pruning wins).
func TestWalkR1SidecarKeptStrayDropped(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{
		"Movies/Film/Film.mp4",
		"Movies/Film/._Film.nfo",          // excluded (._*) but sidecar -> KEEP
		"Movies/Film/._orphan.txt",        // excluded, classify() claims nothing -> DROP
		"project/node_modules/._Film.nfo", // sidecar-claimed but under PRUNED dir -> GONE
	})
	got := walkRels(t, root, []string{"os_metadata", "node_modules_build"}, nil, nil)
	if !hasStr(got, "Movies/Film/Film.mp4") {
		t.Error("primary media should be present")
	}
	if !hasStr(got, "Movies/Film/._Film.nfo") {
		t.Error("R1: sidecar should be kept despite ._* exclusion")
	}
	if hasStr(got, "Movies/Film/._orphan.txt") {
		t.Error("stray excluded non-sidecar should be dropped")
	}
	for _, r := range got {
		if contains(r, "node_modules") {
			t.Errorf("sidecar under pruned dir leaked: %q", r)
		}
	}
}

func TestWalkScopedStartRel(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a/one.mp4", "b/two.mp4", "b/sub/three.mp4"})
	spec := BuildLibrarySpec(nil, nil, nil)
	var got []string
	if err := Walk(root, "b", spec, func(e WalkEntry) error {
		got = append(got, e.Rel)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	sort.Strings(got)
	// rel stays relative to root, only b/ and below visited.
	if !equalStrs(got, []string{"b/sub/three.mp4", "b/two.mp4"}) {
		t.Errorf("scoped walk should only visit b/, got %v", got)
	}
}

func TestWalkCachedirTagPruned(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"real.mp4", "cachedir/data.bin"})
	// Tag the cachedir with the exact bford.info signature.
	tag := filepath.Join(root, "cachedir", cachedirTagName)
	if err := os.WriteFile(tag, []byte(cachedirTagSignature+"\n# comment"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := walkRels(t, root, []string{"-hidden_dotfiles"}, nil, nil)
	for _, r := range got {
		if contains(r, "cachedir") {
			t.Errorf("CACHEDIR.TAG-tagged dir should be pruned, leaked: %q", r)
		}
	}
	if !hasStr(got, "real.mp4") {
		t.Error("real file should survive")
	}
}

func contains(s, sub string) bool { return strings.Contains(s, sub) }

func hasStr(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
