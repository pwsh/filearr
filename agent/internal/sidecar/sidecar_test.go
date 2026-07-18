package sidecar

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// fakeStat builds a stat function that reports the given set of paths as
// existing and everything else as missing.
func fakeStat(exist ...string) func(string) (os.FileInfo, error) {
	set := map[string]bool{}
	for _, p := range exist {
		set[p] = true
	}
	return func(p string) (os.FileInfo, error) {
		if set[p] {
			return dummyInfo{}, nil
		}
		return nil, os.ErrNotExist
	}
}

type dummyInfo struct{}

func (dummyInfo) Name() string       { return "x" }
func (dummyInfo) Size() int64        { return 0 }
func (dummyInfo) Mode() os.FileMode  { return 0 }
func (dummyInfo) ModTime() time.Time { return time.Time{} }
func (dummyInfo) IsDir() bool        { return false }
func (dummyInfo) Sys() any           { return nil }

func TestDiscoverOrder(t *testing.T) {
	besideExe := filepath.Join("/opt/app", FileName)
	osPath := OSConfigPath("linux", func(string) string { return "" }) // /etc/filearr-agent/filearr-agent.json

	cases := []struct {
		name     string
		r        Resolver
		wantPath string
		wantOK   bool
	}{
		{
			name:     "explicit wins even if missing",
			r:        Resolver{Explicit: "/custom/cfg.json", ExePath: "/opt/app/filearr-agent", GOOS: "linux", stat: fakeStat(besideExe, osPath)},
			wantPath: "/custom/cfg.json",
			wantOK:   true,
		},
		{
			name:     "beside exe when it exists",
			r:        Resolver{ExePath: "/opt/app/filearr-agent", GOOS: "linux", stat: fakeStat(besideExe, osPath)},
			wantPath: besideExe,
			wantOK:   true,
		},
		{
			name:     "os config dir when beside-exe absent",
			r:        Resolver{ExePath: "/opt/app/filearr-agent", GOOS: "linux", stat: fakeStat(osPath)},
			wantPath: osPath,
			wantOK:   true,
		},
		{
			name:   "none found",
			r:      Resolver{ExePath: "/opt/app/filearr-agent", GOOS: "linux", stat: fakeStat()},
			wantOK: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := tc.r.Discover()
			if ok != tc.wantOK {
				t.Fatalf("found=%v, want %v (path=%q)", ok, tc.wantOK, got)
			}
			if tc.wantOK && got != tc.wantPath {
				t.Fatalf("path=%q, want %q", got, tc.wantPath)
			}
		})
	}
}

func TestOSConfigPathPerOS(t *testing.T) {
	cases := []struct {
		goos   string
		getenv func(string) string
		want   string
	}{
		{"linux", nil, "/etc/filearr-agent/" + FileName},
		{"darwin", nil, "/Library/Application Support/FilearrAgent/" + FileName},
		{"windows", func(k string) string {
			if k == "ProgramData" {
				return `D:\PD`
			}
			return ""
		}, `D:\PD\Filearr Agent\` + FileName},
		{"windows", func(string) string { return "" }, `C:\ProgramData\Filearr Agent\` + FileName},
	}
	for _, tc := range cases {
		got := OSConfigPath(tc.goos, tc.getenv)
		if got != tc.want {
			t.Fatalf("%s: OSConfigPath=%q, want %q", tc.goos, got, tc.want)
		}
	}
}

func TestLoadToleratesUnknownKeys(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)
	content := `{
  "central_url": "https://filearr.example.com",
  "enrollment_token": "fae_secret_value",
  "log_level": "verbose",
  "future_unknown_key": {"nested": true},
  "another_unknown": 42
}`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := LoadFile(path)
	if err != nil {
		t.Fatalf("LoadFile with unknown keys should not error: %v", err)
	}
	if c.CentralURL != "https://filearr.example.com" || c.LogLevel != "verbose" || c.EnrollmentToken != "fae_secret_value" {
		t.Fatalf("known fields not parsed: %+v", c)
	}
	if _, ok := c.raw["future_unknown_key"]; !ok {
		t.Fatalf("unknown key not preserved in raw: %v", c.raw)
	}
}

func TestConsumeTokenOneShot(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)
	content := `{
  "central_url": "https://filearr.example.com",
  "enrollment_token": "fae_secret_value",
  "config_group": "prod",
  "future_unknown_key": "keep-me"
}`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := LoadFile(path)
	if err != nil {
		t.Fatal(err)
	}

	before := time.Now().Add(-time.Second)
	if err := c.ConsumeToken(time.Now()); err != nil {
		t.Fatalf("ConsumeToken: %v", err)
	}

	// Re-read from disk: the spent token must be gone, a consumed-at marker
	// present, and every other key (including unknown) preserved.
	reloaded, err := LoadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if reloaded.EnrollmentToken != "" {
		t.Fatalf("enrollment_token not cleared: %q", reloaded.EnrollmentToken)
	}
	if reloaded.EnrollmentTokenConsumedAt == "" {
		t.Fatal("enrollment_token_consumed_at marker not stamped")
	}
	ts, perr := time.Parse(time.RFC3339, reloaded.EnrollmentTokenConsumedAt)
	if perr != nil || ts.Before(before) {
		t.Fatalf("bad consumed-at %q (err=%v)", reloaded.EnrollmentTokenConsumedAt, perr)
	}
	if reloaded.ConfigGroup != "prod" {
		t.Fatalf("config_group not preserved: %q", reloaded.ConfigGroup)
	}
	if _, ok := reloaded.raw["future_unknown_key"]; !ok {
		t.Fatal("unknown key lost across rewrite")
	}

	// The spent secret must not appear anywhere in the rewritten file.
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), "fae_secret_value") {
		t.Fatalf("spent token still present in file:\n%s", raw)
	}

	// 0600 at rest (POSIX only — Windows mode bits do not map to an ACL).
	if runtime.GOOS != "windows" {
		info, _ := os.Stat(path)
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("sidecar mode = %v, want 0600", info.Mode().Perm())
		}
	}
}

func TestConsumeTokenNoopWithoutToken(t *testing.T) {
	// No path (config from defaults) => no-op, no error.
	c := &Config{}
	if err := c.ConsumeToken(time.Now()); err != nil {
		t.Fatalf("no-op ConsumeToken errored: %v", err)
	}
	// Path but empty token => still a no-op (no file created/modified).
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)
	c2 := &Config{Path: path}
	if err := c2.ConsumeToken(time.Now()); err != nil {
		t.Fatalf("empty-token ConsumeToken errored: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("ConsumeToken wrote a file when there was no token to consume")
	}
}
