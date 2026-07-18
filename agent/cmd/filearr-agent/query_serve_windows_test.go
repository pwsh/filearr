//go:build windows

package main

import (
	"fmt"
	"net/http"
	"os"
	"sync/atomic"
	"testing"
	"time"

	winio "github.com/Microsoft/go-winio"
)

var servePipeCounter atomic.Int64

// serveLocalTransport starts an HTTP server for handler on a fresh named pipe and
// returns the pipe path (Windows-native, mirroring transport_windows_test.go).
func serveLocalTransport(t *testing.T, handler http.Handler) string {
	t.Helper()
	path := fmt.Sprintf(`\\.\pipe\filearr-querytest-%d-%d`, os.Getpid(), servePipeCounter.Add(1))
	ln, err := winio.ListenPipe(path, nil)
	if err != nil {
		t.Fatalf("ListenPipe(%s): %v", path, err)
	}
	srv := &http.Server{Handler: handler}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close(); _ = ln.Close() })

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if c, derr := winio.DialPipe(path, nil); derr == nil {
			c.Close()
			return path
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("pipe %s never became dialable", path)
	return ""
}
