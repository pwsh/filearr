//go:build linux || darwin

package main

import (
	"net"
	"net/http"
	"path/filepath"
	"testing"
	"time"
)

// serveLocalTransport starts an HTTP server for handler on a fresh Unix domain
// socket and returns the socket path (mirrors transport_unix_test.go).
func serveLocalTransport(t *testing.T, handler http.Handler) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "agent.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatalf("listen unix %s: %v", path, err)
	}
	srv := &http.Server{Handler: handler}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close(); _ = ln.Close() })

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if c, derr := net.Dial("unix", path); derr == nil {
			c.Close()
			return path
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("socket %s never became dialable", path)
	return ""
}
