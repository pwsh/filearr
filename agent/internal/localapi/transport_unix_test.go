//go:build linux || darwin

package localapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
)

// runServer starts s.Run under a cancelable ctx and returns a stop func.
func runServer(t *testing.T, s *Server) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()
	return func() { cancel(); <-done }
}

func newTransportServer(t *testing.T, path string, policy func() PolicyView) *Server {
	t.Helper()
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	idxPath := seedIndex(t, now)
	searcher, err := query.NewSearcher(idxPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })
	countStore, err := index.Open(idxPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { countStore.Close() })
	return New(Config{
		Path:     path,
		Searcher: searcher,
		Count: func(ctx context.Context) (int, error) {
			var n int
			err := countStore.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM items WHERE status='active'`).Scan(&n)
			return n, err
		},
		Policy:       policy,
		GateInterval: 30 * time.Millisecond,
	})
}

func unixClient(path string) *http.Client {
	return &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", path)
		},
	}}
}

func waitDialableUnix(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if c, err := net.Dial("unix", path); err == nil {
			c.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("socket %s never became dialable", path)
}

// TestSocketQueryAndHealth dials the real Unix socket (same-user positive path).
func TestSocketQueryAndHealth(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.sock")
	s := newTransportServer(t, path, nil)
	stop := runServer(t, s)
	defer stop()
	waitDialableUnix(t, path)

	// The socket must not be group/other accessible.
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		t.Fatalf("socket mode %o exposes group/other", fi.Mode().Perm())
	}

	client := unixClient(path)
	body, _ := json.Marshal(QueryRequest{Query: "kind:video"})
	resp, err := client.Post("http://unix/v1/query", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var qr QueryResponse
	json.NewDecoder(resp.Body).Decode(&qr)
	resp.Body.Close()
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("query over socket returned %+v", qr.Rows)
	}

	hresp, err := client.Get("http://unix/v1/health")
	if err != nil {
		t.Fatal(err)
	}
	var h HealthResponse
	json.NewDecoder(hresp.Body).Decode(&h)
	hresp.Body.Close()
	if !h.ReadOnly || !h.IndexReady || h.ItemCount != 4 {
		t.Fatalf("health over socket wrong: %+v", h)
	}
}

// TestStaleSocketCleanup proves platformListen safely unlinks a leftover socket
// file (no live listener) and binds successfully.
func TestStaleSocketCleanup(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.sock")

	// Create a stale socket: listen, disable unlink-on-close, then close — leaving
	// the socket file with no listener behind it.
	l, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	l.(*net.UnixListener).SetUnlinkOnClose(false)
	l.Close()
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("stale socket setup failed: %v", err)
	}

	ln, err := platformListen(path)
	if err != nil {
		t.Fatalf("platformListen must clean a stale socket and bind: %v", err)
	}
	ln.Close()
}

// TestSameUIDSeam is the NEGATIVE peer-auth unit seam: a UID other than the
// agent's own is rejected. A true cross-user integration test needs a second OS
// account (future Linux CI).
func TestSameUIDSeam(t *testing.T) {
	self := uint32(os.Getuid())
	if !sameUID(self) {
		t.Fatal("agent's own UID must pass")
	}
	if sameUID(self + 1) {
		t.Fatal("a different UID must be rejected")
	}
}
