package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/sidecar"
)

func TestScanFlagValue(t *testing.T) {
	cases := []struct {
		name string
		args []string
		flag string
		want string
		ok   bool
	}{
		{"space single dash", []string{"-log-level", "debug"}, "log-level", "debug", true},
		{"space double dash", []string{"--log-level", "verbose"}, "log-level", "verbose", true},
		{"equals single dash", []string{"-log-level=warn"}, "log-level", "warn", true},
		{"equals double dash", []string{"--log-level=error"}, "log-level", "error", true},
		{"absent", []string{"-data", "/x"}, "log-level", "", false},
		{"trailing no value", []string{"-log-level"}, "log-level", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := scanFlagValue(tc.args, tc.flag)
			if got != tc.want || ok != tc.ok {
				t.Fatalf("scanFlagValue(%v,%q)=(%q,%v), want (%q,%v)", tc.args, tc.flag, got, ok, tc.want, tc.ok)
			}
		})
	}
}

// writeSidecar writes a sidecar and points FILEARR_AGENT_CONFIG at it for the test.
func writeSidecar(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, sidecar.FileName)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(sidecar.EnvConfigPath, path)
	return path
}

// TestPrecedenceSidecarThenEnvThenFlag exercises the documented precedence
// (flag > env > sidecar > default) for a common-flag field (central URL).
func TestPrecedenceSidecarThenEnvThenFlag(t *testing.T) {
	writeSidecar(t, `{"central_url":"https://sidecar.example.com"}`)

	// Sidecar only. bindCommonFlags seeds the flag default from env>sidecar>built-in;
	// with no env/flag the sidecar value is the resolved default.
	setupRuntime("test", nil)
	cfg := bindCommonFlags(newFlagSet("t1"))
	if cfg.CentralURL != "https://sidecar.example.com" {
		t.Fatalf("sidecar precedence: central=%q", cfg.CentralURL)
	}

	// Env overrides sidecar.
	t.Setenv(envCentralURL, "https://env.example.com")
	setupRuntime("test", nil)
	fs2 := newFlagSet("t2")
	cfg2 := bindCommonFlags(fs2)
	if err := fs2.Parse(nil); err != nil {
		t.Fatal(err)
	}
	if cfg2.CentralURL != "https://env.example.com" {
		t.Fatalf("env precedence: central=%q", cfg2.CentralURL)
	}

	// Explicit flag overrides env + sidecar.
	setupRuntime("test", nil)
	fs3 := newFlagSet("t3")
	cfg3 := bindCommonFlags(fs3)
	if err := fs3.Parse([]string{"-central", "https://flag.example.com"}); err != nil {
		t.Fatal(err)
	}
	if cfg3.CentralURL != "https://flag.example.com" {
		t.Fatalf("flag precedence: central=%q", cfg3.CentralURL)
	}
}

// TestSidecarLoadedIntoActive confirms setupRuntime populates activeSidecar with
// every recognised field so downstream commands can read them.
func TestSidecarLoadedIntoActive(t *testing.T) {
	writeSidecar(t, `{
      "central_url":"https://c.example.com",
      "enrollment_token":"fae_tok",
      "agent_name":"box-1",
      "config_group":"prod",
      "log_level":"verbose"
    }`)
	setupRuntime("test", nil)
	sc := activeSidecar()
	if sc.CentralURL != "https://c.example.com" || sc.EnrollmentToken != "fae_tok" ||
		sc.AgentName != "box-1" || sc.ConfigGroup != "prod" || sc.LogLevel != "verbose" {
		t.Fatalf("sidecar not fully loaded: %+v", sc)
	}
}
