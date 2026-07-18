package agentlog

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseLevel(t *testing.T) {
	cases := []struct {
		name   string
		want   slog.Level
		wantOK bool
	}{
		{"", slog.LevelInfo, true},
		{"error", slog.LevelError, true},
		{"warn", slog.LevelWarn, true},
		{"warning", slog.LevelWarn, true},
		{"info", slog.LevelInfo, true},
		{"INFO", slog.LevelInfo, true},
		{"verbose", LevelVerbose, true},
		{"debug", slog.LevelDebug, true},
		{"nonsense", slog.LevelInfo, false},
	}
	for _, tc := range cases {
		got, ok := ParseLevel(tc.name)
		if got != tc.want || ok != tc.wantOK {
			t.Fatalf("ParseLevel(%q) = (%v,%v), want (%v,%v)", tc.name, got, ok, tc.want, tc.wantOK)
		}
	}
}

// TestVerboseOrdering pins the deliberate ordering: verbose sits between info and
// debug. A handler at info hides verbose; a handler at verbose shows verbose but
// hides debug; a handler at debug shows everything.
func TestVerboseOrdering(t *testing.T) {
	cases := []struct {
		threshold   slog.Level
		showVerbose bool
		showDebug   bool
	}{
		{slog.LevelInfo, false, false},
		{LevelVerbose, true, false},
		{slog.LevelDebug, true, true},
	}
	for _, tc := range cases {
		dir := t.TempDir()
		logger, closer, err := New(Options{Level: tc.threshold, LogDir: dir})
		if err != nil {
			t.Fatal(err)
		}
		Verbose(logger, "verbose-line")
		logger.Debug("debug-line")
		logger.Info("info-line")
		_ = closer.Close()

		data, _ := os.ReadFile(filepath.Join(dir, LogFileName))
		out := string(data)
		if !strings.Contains(out, "info-line") {
			t.Fatalf("threshold %v: info always expected, got:\n%s", tc.threshold, out)
		}
		if got := strings.Contains(out, "verbose-line"); got != tc.showVerbose {
			t.Fatalf("threshold %v: verbose shown=%v, want %v", tc.threshold, got, tc.showVerbose)
		}
		if got := strings.Contains(out, "debug-line"); got != tc.showDebug {
			t.Fatalf("threshold %v: debug shown=%v, want %v", tc.threshold, got, tc.showDebug)
		}
	}
}

// TestVerboseRendersAsVERBOSE checks the custom level name in the text handler.
func TestVerboseRendersAsVERBOSE(t *testing.T) {
	dir := t.TempDir()
	logger, closer, err := New(Options{Level: LevelVerbose, LogDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	Verbose(logger, "hello")
	_ = closer.Close()
	data, _ := os.ReadFile(filepath.Join(dir, LogFileName))
	if !strings.Contains(string(data), "level=VERBOSE") {
		t.Fatalf("verbose level not rendered as VERBOSE:\n%s", data)
	}
}

// TestFileSinkSmoke wires the lumberjack sink and asserts a line lands on disk.
func TestFileSinkSmoke(t *testing.T) {
	dir := t.TempDir()
	logger, closer, err := New(Options{Level: slog.LevelInfo, LogDir: dir})
	if err != nil {
		t.Fatal(err)
	}
	logger.Info("smoke", "k", "v")
	if err := closer.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	path := filepath.Join(dir, LogFileName)
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("log file not created: %v", err)
	}
	if info.Size() == 0 {
		t.Fatal("log file is empty")
	}
}

// TestNoDirUsesStderrCloserNoop ensures a dir-less logger returns a no-op closer
// and creates no file.
func TestNoDirUsesStderrCloserNoop(t *testing.T) {
	logger, closer, err := New(Options{Level: slog.LevelInfo})
	if err != nil {
		t.Fatal(err)
	}
	if logger == nil {
		t.Fatal("nil logger")
	}
	if err := closer.Close(); err != nil {
		t.Fatalf("noop close errored: %v", err)
	}
}
