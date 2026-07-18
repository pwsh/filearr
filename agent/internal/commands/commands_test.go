package commands

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/scan"
)

// completeRecord captures one /complete call.
type completeRecord struct {
	OK     bool           `json:"ok"`
	Result map[string]any `json:"result"`
}

// mockCentral is a minimal in-memory stand-in for the agent-command plane
// (poll/ack/complete), matching backend/filearr/api/agent_commands.py routes.
type mockCentral struct {
	mu        sync.Mutex
	queued    []commandOut // returned by the next poll, then drained
	polls     int
	acks      map[string]int
	completes map[string]completeRecord
	failPoll  bool // return 500 on poll (central-error path)
}

func newMockCentral() *mockCentral {
	return &mockCentral{acks: map[string]int{}, completes: map[string]completeRecord{}}
}

func (m *mockCentral) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		switch {
		case strings.HasSuffix(p, "/commands/poll"):
			m.mu.Lock()
			m.polls++
			fail := m.failPoll
			out := m.queued
			m.queued = nil
			m.mu.Unlock()
			if fail {
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"detail":"boom"}`))
				return
			}
			_ = json.NewEncoder(w).Encode(out)
		case strings.HasSuffix(p, "/ack"):
			cid := segBefore(p, "/ack")
			m.mu.Lock()
			m.acks[cid]++
			m.mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"id": cid, "status": "picked_up"})
		case strings.HasSuffix(p, "/complete"):
			cid := segBefore(p, "/complete")
			var rec completeRecord
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &rec)
			m.mu.Lock()
			m.completes[cid] = rec
			m.mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]any{"id": cid, "status": "done"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
}

// segBefore returns the path segment immediately before suffix (the command id).
func segBefore(path, suffix string) string {
	trimmed := strings.TrimSuffix(path, suffix)
	i := strings.LastIndex(trimmed, "/")
	return trimmed[i+1:]
}

func (m *mockCentral) completeFor(cid string) (completeRecord, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.completes[cid]
	return rec, ok
}

func (m *mockCentral) ackCount(cid string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.acks[cid]
}

func newTestPoller(srv *httptest.Server, ex *Executor, lease int) *Poller {
	return NewPoller(Config{
		BaseURL:      srv.URL,
		AgentID:      "agent-1",
		AuthFn:       func() string { return "fp" },
		HTTP:         srv.Client(),
		Executor:     ex,
		LeaseSeconds: lease,
	})
}

func TestPollExecuteCompleteStatAndRehash(t *testing.T) {
	root := t.TempDir()
	full := writeFile(t, root, "movie.mkv", []byte("some movie bytes here"))
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)

	m := newMockCentral()
	m.queued = []commandOut{
		{ID: "c-stat", Kind: KindStatCheck, ItemID: "i1", Payload: map[string]any{"library_ref": root, "rel_path": "movie.mkv"}},
		{ID: "c-rehash", Kind: KindRehashCheck, ItemID: "i1", Payload: map[string]any{"library_ref": root, "rel_path": "movie.mkv", "content": true}},
	}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	n, err := newTestPoller(srv, ex, 300).PollOnce(context.Background())
	if err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if n != 2 {
		t.Fatalf("expected 2 commands processed, got %d", n)
	}

	// stat: exists + size, no hashes.
	stat, ok := m.completeFor("c-stat")
	if !ok || !stat.OK || stat.Result["exists"] != true {
		t.Fatalf("bad stat complete: %+v", stat)
	}
	if _, hasQuick := stat.Result["quick_hash"]; hasQuick {
		t.Fatalf("stat must not carry a hash: %+v", stat.Result)
	}

	// rehash: hashes match the scan helpers exactly.
	rh, ok := m.completeFor("c-rehash")
	if !ok || !rh.OK {
		t.Fatalf("bad rehash complete: %+v", rh)
	}
	wantQuick, _ := scan.QuickHash(full, 21)
	wantContent, _ := scan.FullHash(full)
	if rh.Result["quick_hash"] != wantQuick || rh.Result["content_hash"] != wantContent {
		t.Fatalf("rehash hashes mismatch: got %+v want quick=%s content=%s", rh.Result, wantQuick, wantContent)
	}
}

func TestUnknownAndStageUploadRefused(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	m := newMockCentral()
	m.queued = []commandOut{
		{ID: "c-stage", Kind: KindStageUpload, ItemID: "i1", Payload: map[string]any{}},
		{ID: "c-weird", Kind: "frobnicate", ItemID: "i1", Payload: map[string]any{}},
	}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	if _, err := newTestPoller(srv, ex, 300).PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	for _, cid := range []string{"c-stage", "c-weird"} {
		rec, ok := m.completeFor(cid)
		if !ok || rec.OK {
			t.Fatalf("%s should complete ok=false: %+v", cid, rec)
		}
		if _, has := rec.Result["error"]; !has {
			t.Fatalf("%s should carry an error note: %+v", cid, rec.Result)
		}
	}
}

func TestLeaseHeartbeatDuringSlowHash(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "slow.bin", []byte("payload"))
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	// Inject a deliberately-slow content hash so the rehash outlasts lease/3.
	ex.fullHash = func(string) (string, error) {
		time.Sleep(700 * time.Millisecond)
		return "deadbeef", nil
	}

	m := newMockCentral()
	m.queued = []commandOut{
		{ID: "c-slow", Kind: KindRehashCheck, ItemID: "i1", Payload: map[string]any{"library_ref": root, "rel_path": "slow.bin", "content": true}},
	}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	// lease=1s => heartbeat every ~333ms; a ~700ms hash yields at least one ack.
	if _, err := newTestPoller(srv, ex, 1).PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if got := m.ackCount("c-slow"); got < 1 {
		t.Fatalf("expected >=1 lease heartbeat ack during slow hash, got %d", got)
	}
	rec, ok := m.completeFor("c-slow")
	if !ok || !rec.OK || rec.Result["content_hash"] != "deadbeef" {
		t.Fatalf("slow rehash should still complete: %+v", rec)
	}
}

func TestBackoffOnCentralError(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	m := newMockCentral()
	m.failPoll = true
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	if _, err := newTestPoller(srv, ex, 300).PollOnce(context.Background()); err == nil {
		t.Fatal("expected an error when central returns 500 on poll")
	}
}

func TestBackoffOnCentralDown(t *testing.T) {
	root := t.TempDir()
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	// A closed server => connection refused (central-down).
	srv := httptest.NewServer(newMockCentral().handler())
	url := srv.URL
	srv.Close()
	p := NewPoller(Config{BaseURL: url, AgentID: "agent-1", AuthFn: func() string { return "fp" }, Executor: ex})
	if _, err := p.PollOnce(context.Background()); err == nil {
		t.Fatal("expected an error when central is unreachable")
	}
}
