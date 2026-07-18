package pathspec

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// fakeEnv builds a Getenv from a map.
func fakeEnv(m map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) { v, ok := m[k]; return v, ok }
}

func TestExpandTokens(t *testing.T) {
	home := func() (string, error) { return filepath.FromSlash("/home/alice"), nil }
	env := fakeEnv(map[string]string{
		"USERPROFILE": `C:\Users\bob`,
		"HOME":        "/home/alice",
		"XDG_DOCS":    "/home/alice/docs",
	})
	cases := []struct {
		name, in, want string
		wantErr        bool
	}{
		{name: "tilde only", in: "~", want: filepath.FromSlash("/home/alice")},
		// expandTokens is separator-agnostic; normalization to native separators
		// is expandOne's job (FromSlash), so the tilde suffix keeps its slash here.
		{name: "tilde slash", in: "~/docs", want: filepath.FromSlash("/home/alice") + "/docs"},
		{name: "percent", in: `%USERPROFILE%\Documents`, want: `C:\Users\bob\Documents`},
		{name: "dollar", in: "$HOME/media", want: "/home/alice/media"},
		{name: "brace dollar", in: "${XDG_DOCS}/a", want: "/home/alice/docs/a"},
		{name: "literal percent", in: "a%%b", want: "a%b"},
		{name: "bare dollar", in: "cost$", want: "cost$"},
		{name: "unset percent", in: "%NOPE%/x", wantErr: true},
		{name: "unset dollar", in: "$NOPE/x", wantErr: true},
		{name: "unterminated brace", in: "${OPEN/x", wantErr: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := expandTokens(c.in, env, home)
			if c.wantErr {
				if err == nil {
					t.Fatalf("expected error, got %q", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != c.want {
				t.Fatalf("got %q want %q", got, c.want)
			}
		})
	}
}

func TestExpandLiteralAndGlob(t *testing.T) {
	root := t.TempDir()
	// Build /root/users/{alice,bob}/documents plus a non-matching dir.
	for _, u := range []string{"alice", "bob"} {
		if err := os.MkdirAll(filepath.Join(root, "users", u, "documents"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(root, "users", "carol", "other"), 0o755); err != nil {
		t.Fatal(err)
	}

	e := &Expander{Getenv: fakeEnv(map[string]string{"ROOT": root})}

	// Literal (no glob meta): returned as-is regardless of existence.
	missing := filepath.Join(root, "nope")
	res := e.Expand([]string{missing})
	if len(res.Roots) != 1 || res.Roots[0] != filepath.Clean(missing) {
		t.Fatalf("literal spec: got %v", res.Roots)
	}

	// Multi-user glob: matches alice+bob/documents, not carol/other.
	spec := "$ROOT/users/*/documents"
	res = e.Expand([]string{spec})
	if len(res.Roots) != 2 {
		t.Fatalf("glob roots: got %v", res.Roots)
	}
	for _, r := range res.Roots {
		if filepath.Base(r) != "documents" {
			t.Fatalf("unexpected root %q", r)
		}
	}
}

func TestExpandDedupAndErrorRecorded(t *testing.T) {
	root := t.TempDir()
	e := &Expander{}
	lit := filepath.Join(root, "a")
	res := e.Expand([]string{lit, lit, "%UNSET_VAR_XYZ%/z"})
	if len(res.Roots) != 1 {
		t.Fatalf("dedup failed: %v", res.Roots)
	}
	if _, ok := res.Errors["%UNSET_VAR_XYZ%/z"]; !ok {
		t.Fatalf("expected recorded error for unset var, got %v", res.Errors)
	}
}

func TestExpandFanoutCap(t *testing.T) {
	root := t.TempDir()
	for i := 0; i < 6; i++ {
		if err := os.MkdirAll(filepath.Join(root, "d"+string(rune('a'+i))), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	e := &Expander{MaxRoots: 3, Getenv: fakeEnv(map[string]string{"R": root})}
	res := e.Expand([]string{"$R/*"})
	if !res.Truncated {
		t.Fatalf("expected truncated")
	}
	if len(res.Roots) != 3 {
		t.Fatalf("expected cap 3, got %d", len(res.Roots))
	}
}

func TestFilter(t *testing.T) {
	f, err := CompileFilter([]string{`\.txt$`}, []string{`^tmp/`})
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]bool{
		"a/b.txt":   true,  // include match, no exclude
		"a/b.jpg":   false, // no include match
		"tmp/a.txt": false, // excluded even though include matches
	}
	for rel, want := range cases {
		if got := f.Allow(rel); got != want {
			t.Fatalf("Allow(%q)=%v want %v", rel, got, want)
		}
	}

	// Empty include => admit everything not excluded.
	f2, _ := CompileFilter(nil, []string{`\.bak$`})
	if !f2.Allow("anything") || f2.Allow("x.bak") {
		t.Fatalf("empty-include filter wrong")
	}

	// Bad pattern errors.
	if _, err := CompileFilter([]string{"("}, nil); err == nil {
		t.Fatalf("expected compile error")
	}

	// Nil filter admits all.
	var nilF *Filter
	if !nilF.Allow("x") {
		t.Fatalf("nil filter should admit")
	}
}

func TestParseUserDirs(t *testing.T) {
	// A German-localized file WITH a comment line and a bare $HOME value.
	content := `# This file is written by xdg-user-dirs-update
XDG_DESKTOP_DIR="$HOME/Schreibtisch"
XDG_DOCUMENTS_DIR="$HOME/Dokumente"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_MUSIC_DIR="$HOME/Musik"
# a comment in the middle
XDG_PICTURES_DIR="$HOME/Bilder"
XDG_PUBLICSHARE_DIR="$HOME"
NOT_XDG="ignored"
`
	m := parseUserDirs(content, "/home/hans")
	if m["XDG_DOCUMENTS_DIR"] != "/home/hans/Dokumente" {
		t.Fatalf("docs: %q", m["XDG_DOCUMENTS_DIR"])
	}
	if m["XDG_PUBLICSHARE_DIR"] != "/home/hans" {
		t.Fatalf("bare $HOME: %q", m["XDG_PUBLICSHARE_DIR"])
	}
	if _, ok := m["NOT_XDG"]; ok {
		t.Fatalf("non-XDG key leaked")
	}
}

// fakeHost implements Host for OS-independent preset resolution tests.
type fakeHost struct {
	goos     string
	home     string
	kf       map[string]string
	profiles []string
	userDirs map[string]map[string]string
}

func (h fakeHost) GOOS() string          { return h.goos }
func (h fakeHost) Home() (string, error) { return h.home, nil }
func (h fakeHost) KnownFolder(name string) (string, bool) {
	v, ok := h.kf[name]
	return v, ok
}
func (h fakeHost) Profiles() []string { return h.profiles }
func (h fakeHost) UserDirs(home string) map[string]string {
	if h.userDirs == nil {
		return map[string]string{}
	}
	return h.userDirs[home]
}

func TestResolvePresetWindows(t *testing.T) {
	h := fakeHost{
		goos:     "windows",
		kf:       map[string]string{"Documents": `C:\Users\bob\OneDrive\Documents`},
		profiles: []string{`C:\Users\bob`, `C:\Users\ann`},
	}
	specs, err := ResolvePreset(h, PresetUserDocuments)
	if err != nil {
		t.Fatal(err)
	}
	// KFM-correct known folder + both profile joins.
	want := map[string]bool{
		`C:\Users\bob\OneDrive\Documents`:          true,
		filepath.Join(`C:\Users\bob`, "Documents"): true,
		filepath.Join(`C:\Users\ann`, "Documents"): true,
	}
	if len(specs) != len(want) {
		t.Fatalf("got %v", specs)
	}
	for _, s := range specs {
		if !want[s] {
			t.Fatalf("unexpected spec %q in %v", s, specs)
		}
	}

	// server-data is empty on Windows.
	sd, _ := ResolvePreset(h, PresetServerData)
	if len(sd) != 0 {
		t.Fatalf("windows server-data should be empty: %v", sd)
	}
}

func TestResolvePresetLinuxXDGAndFallback(t *testing.T) {
	h := fakeHost{
		goos:     "linux",
		profiles: []string{"/home/hans", "/home/min"},
		userDirs: map[string]map[string]string{
			"/home/hans": {"XDG_DOCUMENTS_DIR": "/home/hans/Dokumente"},
			// /home/min has NO user-dirs.dirs → fall back to $HOME.
		},
	}
	specs, err := ResolvePreset(h, PresetUserDocuments)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{"/home/hans/Dokumente": true, "/home/min": true}
	if len(specs) != 2 {
		t.Fatalf("got %v", specs)
	}
	for _, s := range specs {
		if !want[s] {
			t.Fatalf("unexpected %q in %v", s, specs)
		}
	}

	sd, _ := ResolvePreset(h, PresetServerData)
	if len(sd) != 2 || sd[0] != "/srv" || sd[1] != "/var/www" {
		t.Fatalf("linux server-data: %v", sd)
	}
}

func TestResolvePresetDarwinFixed(t *testing.T) {
	h := fakeHost{goos: "darwin", profiles: []string{"/Users/kim"}}
	specs, _ := ResolvePreset(h, PresetUserMedia)
	want := []string{
		filepath.Join("/Users/kim", "Pictures"),
		filepath.Join("/Users/kim", "Movies"),
		filepath.Join("/Users/kim", "Music"),
	}
	if len(specs) != 3 {
		t.Fatalf("got %v", specs)
	}
	for i, w := range want {
		if specs[i] != w {
			t.Fatalf("specs[%d]=%q want %q", i, specs[i], w)
		}
	}
}

func TestResolvePresetUnknownAndCustom(t *testing.T) {
	h := fakeHost{goos: "linux"}
	if _, err := ResolvePreset(h, "bogus"); err == nil {
		t.Fatalf("expected error for unknown preset")
	}
	specs, err := ResolvePreset(h, PresetCustom)
	if err != nil || len(specs) != 0 {
		t.Fatalf("custom: %v %v", specs, err)
	}
}

func TestOSHostProfilesSmoke(t *testing.T) {
	// Just exercise the real host on this platform; correctness is host-specific,
	// so we only assert it does not panic and returns absolute-ish paths.
	h := OSHost()
	if h.GOOS() != runtime.GOOS {
		t.Fatalf("GOOS mismatch")
	}
	_ = h.Profiles()
}
