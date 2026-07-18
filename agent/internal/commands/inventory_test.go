package commands

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/pathspec"
)

func TestDecodeInventoryPayload(t *testing.T) {
	raw := map[string]any{
		"collectors":    []any{"stat", "owner"},
		"preset":        "user-documents",
		"paths":         []any{"/data/a", "/data/b"},
		"include_regex": []any{`\.txt$`},
		"exclude_regex": []any{},
		"max_entries":   float64(500), // JSON numbers decode to float64
		"max_depth":     float64(3),
	}
	cmd := decodeInventoryPayload(raw)
	if cmd.Preset != "user-documents" {
		t.Fatalf("preset: %q", cmd.Preset)
	}
	if len(cmd.Collectors) != 2 || cmd.Collectors[0] != "stat" {
		t.Fatalf("collectors: %v", cmd.Collectors)
	}
	if len(cmd.Paths) != 2 {
		t.Fatalf("paths: %v", cmd.Paths)
	}
	if cmd.MaxEntries != 500 || cmd.MaxDepth != 3 {
		t.Fatalf("caps: %d %d", cmd.MaxEntries, cmd.MaxDepth)
	}
	if len(cmd.IncludeRegex) != 1 {
		t.Fatalf("include: %v", cmd.IncludeRegex)
	}
}

// invMockCentral extends the command-plane mock with poll-body capture and the
// inventory-results receiver, for the W6-D3 wiring tests.
type invMockCentral struct {
	*mockCentral
	mu           sync.Mutex
	lastPollBody map[string]any
	invBlobs     map[string][]byte // command-id -> uploaded gzip blob
}

func newInvMock() *invMockCentral {
	return &invMockCentral{mockCentral: newMockCentral(), invBlobs: map[string][]byte{}}
}

func (m *invMockCentral) handler() http.Handler {
	inner := m.mockCentral.handler()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		switch {
		case strings.HasSuffix(p, "/commands/poll"):
			body, _ := io.ReadAll(r.Body)
			var parsed map[string]any
			_ = json.Unmarshal(body, &parsed)
			m.mu.Lock()
			m.lastPollBody = parsed
			m.mu.Unlock()
			r.Body = io.NopCloser(bytes.NewReader(body)) // let the inner poll re-read
			inner.ServeHTTP(w, r)
		case strings.HasSuffix(p, "/inventory-results"):
			cid := r.Header.Get("X-Filearr-Command-Id")
			blob, _ := io.ReadAll(r.Body)
			m.mu.Lock()
			m.invBlobs[cid] = blob
			m.mu.Unlock()
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"result_ref": "inventory/" + cid + ".ndjson.gz"})
		default:
			inner.ServeHTTP(w, r)
		}
	})
}

func newInvPoller(srv *httptest.Server) *Poller {
	return NewPoller(Config{
		BaseURL:      srv.URL,
		AgentID:      "agent-1",
		AuthFn:       func() string { return "fp" },
		HTTP:         srv.Client(),
		Inventory:    inventory.NewRunner(nil, pathspec.OSHost()),
		Capabilities: inventory.Capabilities(),
		LeaseSeconds: 300,
	})
}

func TestInventoryInlineCompletion(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	m := newInvMock()
	m.queued = []commandOut{{
		ID: "c-inv", Kind: KindInventory, ItemID: "i1",
		Payload: map[string]any{"paths": []any{root}, "collectors": []any{"stat"}},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	if _, err := newInvPoller(srv).PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-inv")
	if !ok || !rec.OK {
		t.Fatalf("inventory should complete ok: %+v", rec)
	}
	if _, has := rec.Result["summary"]; !has {
		t.Fatalf("summary missing: %+v", rec.Result)
	}
	entries, ok := rec.Result["entries"].([]any)
	if !ok || len(entries) != 1 {
		t.Fatalf("inline entries wrong: %+v", rec.Result["entries"])
	}

	// The poll body advertised the capabilities.
	m.mu.Lock()
	caps, _ := m.lastPollBody["capabilities"].(map[string]any)
	m.mu.Unlock()
	if caps == nil || caps["inventory_version"] == nil {
		t.Fatalf("capabilities not advertised in poll body: %+v", m.lastPollBody)
	}
}

func TestInventoryUploadCompletion(t *testing.T) {
	root := t.TempDir()
	// Enough entries to exceed a tiny inline cap so the upload path is exercised.
	for i := 0; i < 50; i++ {
		name := "file_" + string(rune('a'+i%26)) + string(rune('a'+i/26)) + ".txt"
		if err := os.WriteFile(filepath.Join(root, name), []byte("data"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	m := newInvMock()
	m.queued = []commandOut{{
		ID: "c-big", Kind: KindInventory, ItemID: "i1",
		Payload: map[string]any{"paths": []any{root}, "collectors": []any{"stat"}},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	// Force the upload path with a 1-byte inline cap.
	p := newInvPoller(srv)
	p.inv = inventory.NewRunnerWithInlineCap(nil, pathspec.OSHost(), 1)

	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-big")
	if !ok || !rec.OK {
		t.Fatalf("big inventory should complete ok: %+v", rec)
	}
	if rec.Result["result_ref"] == nil {
		t.Fatalf("result_ref missing: %+v", rec.Result)
	}
	if _, has := rec.Result["entries"]; has {
		t.Fatalf("upload completion must NOT inline entries: %+v", rec.Result)
	}

	// Central received a gzip blob that decompresses to NDJSON.
	m.mu.Lock()
	blob := m.invBlobs["c-big"]
	m.mu.Unlock()
	if len(blob) == 0 {
		t.Fatalf("no blob uploaded")
	}
	zr, err := gzip.NewReader(bytes.NewReader(blob))
	if err != nil {
		t.Fatalf("gzip: %v", err)
	}
	nd, _ := io.ReadAll(zr)
	if !bytes.Contains(nd, []byte(`"rel"`)) {
		t.Fatalf("decompressed blob not NDJSON: %q", nd[:min(80, len(nd))])
	}
}
