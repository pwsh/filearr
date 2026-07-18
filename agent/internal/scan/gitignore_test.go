package scan

import "testing"

// gitignoreVector is one row of the P5-T3a spike table (docs/research/
// phase-5-t3a-gitignore-spike.md). rel is the probe WITHOUT a trailing slash;
// isDir replaces the table's trailing-slash directory convention with the
// library's native isDir flag (presets.prune_dir probes dirs with rel+"/").
// excluded is the pathspec-computed ground truth ("excluded"→true, "kept"→false).
type gitignoreVector struct {
	id       string
	patterns []string
	rel      string
	isDir    bool
	excluded bool
}

// vectorGate is the PERMANENT compatibility gate: all 44 spike vectors, verbatim.
// Any git-pkgs/gitignore bump (or central pathspec bump) MUST keep this green
// (CLAUDE.md invariant: the central and agent exclusion catalogs must not
// silently diverge). Ground truth is pathspec==1.1.1's GitIgnoreSpec.from_lines.
var vectorGate = []gitignoreVector{
	{"V01", []string{"*.log"}, "foo.log", false, true},
	{"V02", []string{"*.log"}, "foo.txt", false, false},
	{"V03", []string{"*.log", "!keep.log"}, "keep.log", false, false},
	{"V03b", []string{"*.log", "!keep.log"}, "other.log", false, true},
	{"V04", []string{"*.iso", "!keepme.iso", "keepme.iso"}, "keepme.iso", false, true},
	{"V05", []string{"node_modules/", "!node_modules/keep.txt"}, "node_modules/keep.txt", false, false},
	{"V05b", []string{"node_modules/"}, "node_modules", true, true},
	{"V06", []string{"build/"}, "build", true, true},
	{"V07", []string{"build/"}, "build", false, false},
	{"V08", []string{"build/"}, "build/file.txt", false, true},
	{"V09", []string{"/foo.txt"}, "foo.txt", false, true},
	{"V10", []string{"/foo.txt"}, "sub/foo.txt", false, false},
	{"V11", []string{"foo.txt"}, "foo.txt", false, true},
	{"V12", []string{"foo.txt"}, "sub/foo.txt", false, true},
	{"V13", []string{"a/**/b"}, "a/b", false, true},
	{"V14", []string{"a/**/b"}, "a/x/b", false, true},
	{"V15", []string{"a/**/b"}, "a/x/y/b", false, true},
	{"V16", []string{"a/**/b"}, "a/x/y", false, false},
	{"V17", []string{"**/foo.txt"}, "foo.txt", false, true},
	{"V18", []string{"**/foo.txt"}, "deep/nested/foo.txt", false, true},
	{"V19", []string{"foo/**"}, "foo/bar.txt", false, true},
	{"V20", []string{"foo/**"}, "foo", true, true},
	{"V21", []string{"Secret.txt"}, "Secret.txt", false, true},
	{"V22", []string{"Secret.txt"}, "secret.txt", false, false},
	{"V23", []string{"[Tt]humbs.db"}, "Thumbs.db", false, true},
	{"V24", []string{"[Tt]humbs.db"}, "thumbs.db", false, true},
	{"V25", []string{"[Tt]humbs.db"}, "THUMBS.DB", false, false},
	{"V26", []string{"Icon[\r]"}, "Icon\r", false, true},
	{"V27", []string{"Icon[\r]"}, "Icon", false, false},
	{"V28", []string{"Icon[\r]"}, "IconX", false, false},
	{"V29", []string{`\#*`}, "#emacs-autosave#", false, true},
	{"V30", []string{"#*"}, "#emacs-autosave#", false, false},
	{"V31", []string{"$RECYCLE.BIN/"}, "$RECYCLE.BIN", true, true},
	{"V32", []string{"$RECYCLE.BIN/"}, "$RECYCLE.BIN", false, false},
	{"V33", []string{".Trash-*/"}, ".Trash-1000", true, true},
	{"V34", []string{"._*"}, "._resource", false, true},
	{"V35", []string{"._*"}, "resource", false, false},
	{"V36", []string{".*"}, ".hidden_file", false, true},
	{"V37", []string{".*"}, ".config", true, true},
	{"V38", []string{".*"}, "keep.mp4", false, false},
	{"V39", []string{"*.tmp", "!important.tmp"}, "important.tmp", false, false},
	{"V40", []string{"*.tmp", "!important.tmp"}, "scratch.tmp", false, true},
	{"V41", []string{"node_modules/"}, "src/node_modules_helper.js", false, false},
	{"V42", []string{"*.mkv"}, "movie.mp4", false, false},
}

// matchVec builds a matcher over the raw pattern lines (verbatim, in order) and
// probes rel, mirroring the spike's GitIgnoreSpec.from_lines(patterns)
// .match_file(path). Passing the patterns as extraExcludes with no presets/
// includes composes exactly those lines in order (negations like "!keep.log"
// carry through verbatim).
func matchVec(v gitignoreVector) bool {
	spec := BuildSpec(nil, v.patterns, nil)
	if v.isDir {
		return spec.MatchDir(v.rel)
	}
	return spec.MatchFile(v.rel)
}

func TestGitignoreVectorGate(t *testing.T) {
	if len(vectorGate) != 44 {
		t.Fatalf("vector gate must contain all 44 spike vectors, got %d", len(vectorGate))
	}
	for _, v := range vectorGate {
		got := matchVec(v)
		if got != v.excluded {
			t.Errorf("%s: patterns=%v rel=%q isDir=%v — excluded=%v, want %v",
				v.id, v.patterns, v.rel, v.isDir, got, v.excluded)
		}
	}
}

// --- P2-T2 semantic cases (backend/tests/test_pathspec_semantics_t2.py) ------
// Ported through BuildSpec (== presets.build_exclusion_spec) so the agent's
// preset composition matches central's exactly.

func TestP2T2NegationReincludesPresetExcluded(t *testing.T) {
	spec := BuildSpec([]string{"caches_temp"}, nil, []string{"important.tmp"})
	if !spec.MatchFile("scratch.tmp") {
		t.Error("scratch.tmp should stay excluded")
	}
	if spec.MatchFile("important.tmp") {
		t.Error("important.tmp should be re-included via negation")
	}
}

func TestP2T2NegationOrderingLastMatchWins(t *testing.T) {
	spec := BuildSpec(nil, []string{"*.iso"}, []string{"keepme.iso"})
	if !spec.MatchFile("random.iso") {
		t.Error("random.iso should be excluded")
	}
	if spec.MatchFile("keepme.iso") {
		t.Error("keepme.iso should be re-included")
	}
}

func TestP2T2IconCRLiteralMatches(t *testing.T) {
	spec := BuildSpec([]string{"os_metadata"}, nil, nil)
	if !spec.MatchFile("Icon\r") {
		t.Error("Icon\\r (Finder artifact) should be excluded")
	}
	if !spec.MatchFile("art/Icon\r") {
		t.Error("Icon\\r in any directory should be excluded")
	}
	if spec.MatchFile("Icon") {
		t.Error("Icon without trailing CR should be kept")
	}
	if spec.MatchFile("IconX") {
		t.Error("IconX should be kept")
	}
}

func TestP2T2BuiltinCaseTolerant(t *testing.T) {
	spec := BuildSpec([]string{"system_files", "os_metadata"}, nil, nil)
	for _, name := range []string{"Thumbs.db", "thumbs.db", "Desktop.ini", "desktop.ini"} {
		if !spec.MatchFile(name) {
			t.Errorf("%s should be excluded (bracket-expanded builtin)", name)
		}
		if !spec.MatchFile("a/b/" + name) {
			t.Errorf("a/b/%s should be excluded", name)
		}
	}
}

func TestP2T2UserPatternsCaseSensitive(t *testing.T) {
	spec := BuildSpec(nil, []string{"Secret.txt"}, nil)
	if !spec.MatchFile("Secret.txt") {
		t.Error("Secret.txt should be excluded")
	}
	if spec.MatchFile("secret.txt") {
		t.Error("secret.txt (different case) should be kept — R2 documented gap")
	}
}

func TestP2T2Section16PatternSet(t *testing.T) {
	spec := BuildSpec([]string{"system_files", "os_metadata", "caches_temp"}, nil, nil)
	excluded := []struct {
		rel   string
		isDir bool
	}{
		{"Thumbs.db", false}, {"Thumbs.db:encryptable", false}, {"ehthumbs.db", false},
		{"ehthumbs_vista.db", false}, {"Desktop.ini", false}, {"$RECYCLE.BIN", true},
		{"shortcut.lnk", false}, {".DS_Store", false}, {"Icon\r", false}, {"._hidden", false},
		{".Spotlight-V100", true}, {".Trashes", true}, {".fseventsd", true},
		{".DocumentRevisions-V100", true}, {".TemporaryItems", true}, {"backup~", false},
		{".fuse_hidden0001", false}, {".directory", false}, {".Trash-1000", true},
		{".nfs0001", false}, {"nohup.out", false},
	}
	for _, c := range excluded {
		ok := c.isDir && spec.MatchDir(c.rel) || !c.isDir && spec.MatchFile(c.rel)
		if !ok {
			t.Errorf("§1.6 pattern failed to match %q (isDir=%v)", c.rel, c.isDir)
		}
	}
	for _, keep := range []string{"Movies/Arcane/s01e01.mkv", "Music/song.flac", "Docs/manual.pdf"} {
		if spec.MatchFile(keep) {
			t.Errorf("real media wrongly excluded: %q", keep)
		}
	}
}

func TestP2T2IncludeNegationVsAllowlistDivergence(t *testing.T) {
	// The load-bearing migration: include_globs is a negation, not an allowlist.
	// A non-matching file with nothing excluding it is KEPT (old allowlist dropped it).
	spec := BuildSpec(nil, nil, []string{"*.mkv"})
	if spec.MatchFile("movie.mp4") {
		t.Error("movie.mp4 should be KEPT (include is a negation, not an allowlist)")
	}
}

// --- resolveEffectivePresets (test_presets_walk_t1.py helper cases) ----------

func TestResolveEffectivePresetsDefaultOn(t *testing.T) {
	if got := resolveEffectivePresets(nil); len(got) != 1 || got[0] != "hidden_dotfiles" {
		t.Errorf("empty config should resolve to [hidden_dotfiles], got %v", got)
	}
}

func TestResolveEffectivePresetsUnionCanonicalOrder(t *testing.T) {
	got := resolveEffectivePresets([]string{"node_modules_build", "system_files"})
	want := []string{"system_files", "hidden_dotfiles", "node_modules_build"}
	if !equalStrs(got, want) {
		t.Errorf("union should be canonical order %v, got %v", want, got)
	}
}

func TestResolveEffectivePresetsNegativeSentinel(t *testing.T) {
	if got := resolveEffectivePresets([]string{"-hidden_dotfiles"}); len(got) != 0 {
		t.Errorf("-hidden_dotfiles should disable the default, got %v", got)
	}
	got := resolveEffectivePresets([]string{"-hidden_dotfiles", "os_metadata"})
	if !equalStrs(got, []string{"os_metadata"}) {
		t.Errorf("expected [os_metadata], got %v", got)
	}
}

func TestResolveEffectivePresetsIgnoresUnknown(t *testing.T) {
	if got := resolveEffectivePresets([]string{"bogus", "-alsobogus"}); !equalStrs(got, []string{"hidden_dotfiles"}) {
		t.Errorf("unknown names should be ignored, got %v", got)
	}
}

func equalStrs(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
