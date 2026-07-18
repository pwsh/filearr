package main

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// withCapturedStdout redirects os.Stdout for the duration of fn (legacy handlers
// print there directly), stores the captured text in *sink, and returns fn's
// error. Used by the dispatch smoke test to keep runs quiet while still asserting
// on returned errors.
func withCapturedStdout(t *testing.T, sink *string, fn func() error) error {
	t.Helper()
	orig := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	runErr := fn()
	w.Close()
	os.Stdout = orig
	buf, _ := io.ReadAll(r)
	*sink = string(buf)
	return runErr
}

// deadSocketPath returns a socket/pipe path that no agent is listening on, so a
// dial against it fails deterministically (the transport-down test path).
func deadSocketPath(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		return fmt.Sprintf(`\\.\pipe\filearr-agent-dead-%d`, os.Getpid())
	}
	return filepath.Join(t.TempDir(), "dead.sock")
}
