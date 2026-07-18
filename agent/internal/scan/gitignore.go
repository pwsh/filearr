package scan

import (
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/git-pkgs/gitignore"
)

// cachedirTagName / cachedirTagSignature implement the bford.info CACHEDIR.TAG
// spec (backend/filearr/presets.py): a directory holding a file literally named
// CACHEDIR.TAG whose FIRST 43 BYTES are exactly this signature is a regenerable
// cache directory and is pruned. The signature check (not mere presence) is the
// false-positive guard.
const (
	cachedirTagName      = "CACHEDIR.TAG"
	cachedirTagSignature = "Signature: 8a477f597d28d172789f06886806bc55"
)

// Spec is the compiled exclusion matcher for one root, wrapping the
// git-pkgs/gitignore Matcher chosen by P5-T3a (v1.2.0, exact pin — sole 44/44
// on the vector gate). It replaces central's pathspec.GitIgnoreSpec with the
// SAME composition order and probe convention.
type Spec struct {
	m *gitignore.Matcher
}

// BuildSpec composes one Spec from enabled presets + per-root excludes +
// includes, mirroring presets.build_exclusion_spec exactly:
//
//	preset excludes (canonical order, only the enabled ones)
//	  → extraExcludes (library.exclude_globs)
//	  → includes (library.include_globs) as gitignore negations (!pattern)
//
// gitignore last-match-wins semantics mean a later negation re-admits a file an
// earlier preset excluded. Patterns are fed to the Matcher in this exact order.
func BuildSpec(enabledPresets, extraExcludes, includes []string) *Spec {
	var lines []string
	for _, name := range presetOrder(enabledPresets) {
		lines = append(lines, presetByName[name].exclude...)
	}
	lines = append(lines, extraExcludes...)
	for _, inc := range includes {
		if strings.HasPrefix(inc, "!") {
			lines = append(lines, inc)
		} else {
			lines = append(lines, "!"+inc)
		}
	}
	m := gitignore.New("") // empty root: programmatic patterns only, no fs load
	// Join with newlines so the Matcher parses one pattern per line in order;
	// last-match-wins depends on insertion order, which AddPatterns preserves.
	m.AddPatterns([]byte(strings.Join(lines, "\n")+"\n"), "")
	return &Spec{m: m}
}

// BuildLibrarySpec resolves enabledPresets into the effective set then delegates
// to BuildSpec. Mirrors presets.build_library_spec.
func BuildLibrarySpec(enabledPresets, extraExcludes, includes []string) *Spec {
	return BuildSpec(resolveEffectivePresets(enabledPresets), extraExcludes, includes)
}

// presetOrder returns the enabled preset names in canonical order.
func presetOrder(enabledPresets []string) []string {
	want := map[string]bool{}
	for _, n := range enabledPresets {
		want[n] = true
	}
	var out []string
	for _, b := range presetBundles {
		if want[b.name] {
			out = append(out, b.name)
		}
	}
	return out
}

// MatchFile reports whether rel (a root-relative posix path) is excluded. It is
// the analogue of pathspec's spec.match_file(rel).
func (s *Spec) MatchFile(rel string) bool {
	return s.m.MatchPath(rel, false)
}

// MatchDir reports whether the directory rel is pruned by a directory-only
// pattern. It is the analogue of spec.match_file(rel + "/") — presets.prune_dir
// probes directories with the trailing-slash / isDir convention so dir-only
// patterns (node_modules/, $RECYCLE.BIN/, ...) fire.
func (s *Spec) MatchDir(rel string) bool {
	return s.m.MatchPath(rel, true)
}

// PruneDir decides whether the walk should stop descending into a directory,
// mirroring presets.prune_dir: a directory-only gitignore pattern matches OR the
// directory holds a signature-verified CACHEDIR.TAG. Ruling R1: directory
// pruning always wins.
func (s *Spec) PruneDir(rel, absDir string) bool {
	rel = filepath.ToSlash(rel)
	if s.MatchDir(rel) {
		return true
	}
	return isCachedirTagged(absDir)
}

// isCachedirTagged reports whether absDir holds a valid CACHEDIR.TAG. Reads only
// the first 43 bytes and compares them to the exact signature. Any OS error
// (missing file, unreadable) yields false — absence of a valid tag, never an
// error into the walk. Mirrors presets.is_cachedir_tagged.
func isCachedirTagged(absDir string) bool {
	f, err := os.Open(filepath.Join(absDir, cachedirTagName))
	if err != nil {
		return false
	}
	defer f.Close()
	buf := make([]byte, len(cachedirTagSignature))
	n, _ := io.ReadFull(f, buf)
	return string(buf[:n]) == cachedirTagSignature
}
