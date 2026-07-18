//go:build windows

package localapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	winio "github.com/Microsoft/go-winio"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
	"golang.org/x/sys/windows"
)

var pipeCounter atomic.Int64

func uniquePipe(t *testing.T) string {
	t.Helper()
	return fmt.Sprintf(`\\.\pipe\filearr-test-%d-%d`, os.Getpid(), pipeCounter.Add(1))
}

// pipeClient builds an HTTP client that dials the given named pipe.
func pipeClient(path string) *http.Client {
	return &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return winio.DialPipeContext(ctx, path)
		},
	}}
}

// runServer starts s.Run under a cancelable ctx and returns a stop func.
func runServer(t *testing.T, s *Server) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()
	return func() { cancel(); <-done }
}

// waitDialable polls the pipe until a dial succeeds or the deadline passes.
func waitDialable(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		c, err := winio.DialPipe(path, nil)
		if err == nil {
			c.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("pipe %s never became dialable", path)
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

// TestPipeQueryAndHealth dials the real named pipe (same-user positive path) and
// exercises both routes end-to-end over HTTP/1.1.
func TestPipeQueryAndHealth(t *testing.T) {
	path := uniquePipe(t)
	s := newTransportServer(t, path, nil)
	stop := runServer(t, s)
	defer stop()
	waitDialable(t, path)

	client := pipeClient(path)

	// Query.
	body, _ := json.Marshal(QueryRequest{Query: "kind:video", Limit: 5})
	resp, err := client.Post("http://pipe/v1/query", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var qr QueryResponse
	json.NewDecoder(resp.Body).Decode(&qr)
	resp.Body.Close()
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("query over pipe returned %+v", qr.Rows)
	}

	// Health.
	hresp, err := client.Get("http://pipe/v1/health")
	if err != nil {
		t.Fatal(err)
	}
	var h HealthResponse
	json.NewDecoder(hresp.Body).Decode(&h)
	hresp.Body.Close()
	if !h.ReadOnly || !h.IndexReady || h.ItemCount != 4 {
		t.Fatalf("health over pipe wrong: %+v", h)
	}
}

// TestPipePolicyDisableStopsListener proves a live local_access_enabled=false flip
// takes the pipe down within one gate interval, and re-enabling brings it back.
func TestPipePolicyDisableStopsListener(t *testing.T) {
	path := uniquePipe(t)
	var enabled atomic.Bool
	enabled.Store(true)
	s := newTransportServer(t, path, func() PolicyView { return PolicyView{LocalAccessEnabled: enabled.Load()} })
	stop := runServer(t, s)
	defer stop()
	waitDialable(t, path)

	// Disable: the pipe must stop accepting within a few gate intervals.
	enabled.Store(false)
	deadline := time.Now().Add(2 * time.Second)
	down := false
	for time.Now().Before(deadline) {
		if c, err := winio.DialPipe(path, nil); err != nil {
			down = true
			break
		} else {
			c.Close()
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !down {
		t.Fatal("disabling policy did not stop the pipe listener")
	}

	// Re-enable: it comes back.
	enabled.Store(true)
	waitDialable(t, path)
}

// TestPipeRefusesStartWhenDisabled proves the server never opens the pipe when
// policy starts disabled (refuses to start, non-fatal).
func TestPipeRefusesStartWhenDisabled(t *testing.T) {
	path := uniquePipe(t)
	s := newTransportServer(t, path, func() PolicyView { return PolicyView{LocalAccessEnabled: false} })
	stop := runServer(t, s)
	defer stop()

	time.Sleep(200 * time.Millisecond)
	if c, err := winio.DialPipe(path, nil); err == nil {
		c.Close()
		t.Fatal("pipe must not be dialable when policy disables local access")
	}
}

// TestCurrentUserSDDL is the R5 verification: the DACL grants only the current
// user's SID and denies World/Everyone.
func TestCurrentUserSDDL(t *testing.T) {
	sddl, err := currentUserSDDL()
	if err != nil {
		t.Fatal(err)
	}
	self, _ := currentUserSID()
	t.Logf("verified current-user pipe SDDL: %s", sddl)
	if !strings.HasPrefix(sddl, "D:P(A;;GA;;;") {
		t.Fatalf("SDDL not a protected current-user DACL: %s", sddl)
	}
	if !strings.Contains(sddl, self) {
		t.Fatalf("SDDL missing the current-user SID %s: %s", self, sddl)
	}
	// Must NOT grant World ("WD"/S-1-1-0) or Authenticated Users ("AU").
	for _, denied := range []string{";WD)", "S-1-1-0", ";AU)"} {
		if strings.Contains(sddl, denied) {
			t.Fatalf("SDDL grants a forbidden principal %q: %s", denied, sddl)
		}
	}
}

// TestSidEqualRejectsMismatch is the NEGATIVE peer-auth unit seam: a different SID
// (well-known World) must not compare equal to the agent's own SID. A true
// cross-user integration test needs a second OS account (future Windows CI).
func TestSidEqualRejectsMismatch(t *testing.T) {
	self, err := currentUserSID()
	if err != nil {
		t.Fatal(err)
	}
	selfSid, err := windows.StringToSid(self)
	if err != nil {
		t.Fatal(err)
	}
	if !sidEqual(selfSid, self) {
		t.Fatal("self SID must compare equal to itself")
	}
	world, err := windows.CreateWellKnownSid(windows.WinWorldSid)
	if err != nil {
		t.Fatal(err)
	}
	if sidEqual(world, self) {
		t.Fatal("World SID must NOT compare equal to the agent's own SID")
	}
	if sidEqual(nil, self) {
		t.Fatal("nil SID must never match")
	}
}
