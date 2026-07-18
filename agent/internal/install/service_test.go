package install

import "testing"

func TestServiceConfigArgsAndMarker(t *testing.T) {
	layout, _ := ResolveLayout("linux", nil)
	cfg := ServiceConfig(layout, "/etc/filearr-agent/filearr-agent.json", "linux")

	if cfg.Name != ServiceName {
		t.Fatalf("service name=%q, want %q", cfg.Name, ServiceName)
	}
	if cfg.Executable != layout.BinPath {
		t.Fatalf("executable=%q, want %q", cfg.Executable, layout.BinPath)
	}
	// The service env marker lets the running daemon detect service management.
	if cfg.EnvVars[ServiceEnvMarker] != "1" {
		t.Fatalf("env marker %s not set to 1: %v", ServiceEnvMarker, cfg.EnvVars)
	}
	// Arguments must include run + the data/log dirs + the sidecar path.
	want := []string{"run", "--data", layout.DataDir, "--log-dir", layout.LogDir, "--config", "/etc/filearr-agent/filearr-agent.json"}
	if len(cfg.Arguments) != len(want) {
		t.Fatalf("args=%v, want %v", cfg.Arguments, want)
	}
	for i := range want {
		if cfg.Arguments[i] != want[i] {
			t.Fatalf("args=%v, want %v", cfg.Arguments, want)
		}
	}
}

func TestServiceConfigOmitsConfigWhenAbsent(t *testing.T) {
	layout, _ := ResolveLayout("linux", nil)
	cfg := ServiceConfig(layout, "", "linux")
	for _, a := range cfg.Arguments {
		if a == "--config" {
			t.Fatalf("--config should be omitted when no sidecar path: %v", cfg.Arguments)
		}
	}
}

func TestRestartOptionsPerOS(t *testing.T) {
	if got := restartOptions("linux")["Restart"]; got != "on-failure" {
		t.Fatalf("linux Restart=%v, want on-failure", got)
	}
	darwin := restartOptions("darwin")
	if darwin["KeepAlive"] != true || darwin["RunAtLoad"] != true {
		t.Fatalf("darwin options wrong: %v", darwin)
	}
	win := restartOptions("windows")
	if win["OnFailure"] != "restart" {
		t.Fatalf("windows OnFailure=%v, want restart", win["OnFailure"])
	}
}

func TestStatusString(t *testing.T) {
	cases := map[Status]string{
		StatusRunning:      "running",
		StatusStopped:      "stopped",
		StatusNotInstalled: "not installed",
		StatusUnknown:      "unknown",
	}
	for s, want := range cases {
		if s.String() != want {
			t.Fatalf("Status(%d).String()=%q, want %q", s, s.String(), want)
		}
	}
}
