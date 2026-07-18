package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/query"
)

// runQueryCmd drives the real `query` subcommand through the urfave root command,
// capturing stdout/stderr into buffers (so IsTerminal is false → color off →
// no ANSI, exercising the piped path).
func runQueryCmd(t *testing.T, args ...string) (stdout, stderr string, err error) {
	t.Helper()
	var out, errBuf bytes.Buffer
	root := buildRootCommand()
	root.Writer = &out
	root.ErrWriter = &errBuf
	err = root.Run(context.Background(), append([]string{"filearr-agent", "query"}, args...))
	return out.String(), errBuf.String(), err
}

// realIndexHandler seeds a temp index and returns the actual P7-T2 localapi
// handler over it — the "REAL pipe-served temp index" the task requires.
func realIndexHandler(t *testing.T) http.Handler {
	t.Helper()
	idxPath := filepath.Join(t.TempDir(), indexDBName)
	seedCLIIndex(t, idxPath)
	searcher, err := query.NewSearcher(idxPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })
	return localapi.New(localapi.Config{Searcher: searcher}).Handler()
}

// TestQueryNDJSONOverRealIndex: `query --json` yields jq-parseable NDJSON, one
// object per line, snake_case keys — served by the real engine over a real pipe.
func TestQueryNDJSONOverRealIndex(t *testing.T) {
	path := serveLocalTransport(t, realIndexHandler(t))
	out, _, err := runQueryCmd(t, "--socket", path, "--json", "kind:video")
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	lines := nonEmptyLines(out)
	if len(lines) != 1 {
		t.Fatalf("expected 1 NDJSON row, got %d: %q", len(lines), out)
	}
	var row map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &row); err != nil {
		t.Fatalf("NDJSON not jq-parseable: %v (%q)", err, lines[0])
	}
	if row["rel_path"] != "Movies/Film.mkv" {
		t.Errorf("rel_path = %v; want Movies/Film.mkv", row["rel_path"])
	}
}

// TestQueryTableNoANSIWhenPiped: the default (non-json) table carries NO ANSI
// codes when output is not a TTY (a buffer), even without --plain.
func TestQueryTableNoANSIWhenPiped(t *testing.T) {
	path := serveLocalTransport(t, realIndexHandler(t))
	out, _, err := runQueryCmd(t, "--socket", path, "kind:video")
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if strings.Contains(out, "\x1b[") {
		t.Fatalf("piped table output contains ANSI escapes: %q", out)
	}
	for _, want := range []string{"REL PATH", "Movies/Film.mkv"} {
		if !strings.Contains(out, want) {
			t.Errorf("table missing %q:\n%s", want, out)
		}
	}
}

// TestQueryLimitOffsetPassthrough proves --limit/--offset reach the wire request.
func TestQueryLimitOffsetPassthrough(t *testing.T) {
	reqCh := make(chan localapi.QueryRequest, 1)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req localapi.QueryRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		reqCh <- req
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(localapi.QueryResponse{
			Rows:  []localapi.ResultRow{},
			Scope: localapi.ScopeInfo{Predicates: []string{}},
		})
	})
	path := serveLocalTransport(t, handler)

	if _, _, err := runQueryCmd(t, "--socket", path, "--limit", "7", "--offset", "3", "kind:video foo"); err != nil {
		t.Fatalf("query: %v", err)
	}
	got := <-reqCh
	if got.Limit != 7 || got.Offset != 3 {
		t.Errorf("wire request limit/offset = %d/%d; want 7/3", got.Limit, got.Offset)
	}
	if got.Query != "kind:video foo" {
		t.Errorf("wire request query = %q; want %q", got.Query, "kind:video foo")
	}
}

// TestQueryRestrictedViewFooter is the R3 assertion end-to-end: an active scope
// prints the restricted-view footer to stderr (never silent); --verbose lists the
// predicates.
func TestQueryRestrictedViewFooter(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(localapi.QueryResponse{
			Rows:  []localapi.ResultRow{{ID: "1", RelPath: "Movies/ok.mkv", Filename: "ok.mkv", Size: 10, Mtime: "2026-07-17T11:00:00Z"}},
			Total: 1,
			Scope: localapi.ScopeInfo{Active: true, Predicates: []string{"rel_path GLOB 'Movies/*'"}},
		})
	})
	path := serveLocalTransport(t, handler)

	_, stderr, err := runQueryCmd(t, "--socket", path, "kind:video")
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if !strings.Contains(stderr, "restricted view: results are path-scope filtered") {
		t.Fatalf("missing R3 restricted-view footer:\n%s", stderr)
	}
	if strings.Contains(stderr, "rel_path GLOB 'Movies/*'") {
		t.Fatalf("predicates should be hidden without --verbose:\n%s", stderr)
	}

	_, stderrV, err := runQueryCmd(t, "--socket", path, "--verbose", "kind:video")
	if err != nil {
		t.Fatalf("query --verbose: %v", err)
	}
	if !strings.Contains(stderrV, "rel_path GLOB 'Movies/*'") {
		t.Fatalf("--verbose must list scope predicates:\n%s", stderrV)
	}
}

// TestQueryHistoryListing drives `query --history` end-to-end: it records queries
// into a real (separate-file) history store, serves the real localapi handler over
// a real transport, and asserts the frecency-ranked suggestions come back.
func TestQueryHistoryListing(t *testing.T) {
	idxPath := filepath.Join(t.TempDir(), indexDBName)
	seedCLIIndex(t, idxPath)
	searcher, err := query.NewSearcher(idxPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })

	hist, err := history.Open(filepath.Join(t.TempDir(), historyDBName))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hist.Close() })
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		_ = hist.Record(ctx, "kind:video")
	}
	_ = hist.Record(ctx, "notes.txt")

	handler := localapi.New(localapi.Config{Searcher: searcher, History: hist}).Handler()
	path := serveLocalTransport(t, handler)

	out, _, err := runQueryCmd(t, "--socket", path, "--history", "--json")
	if err != nil {
		t.Fatalf("query --history: %v", err)
	}
	lines := nonEmptyLines(out)
	if len(lines) != 2 {
		t.Fatalf("expected 2 history rows, got %d: %q", len(lines), out)
	}
	var first map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &first); err != nil {
		t.Fatalf("history NDJSON not parseable: %v", err)
	}
	if first["query"] != "kind:video" {
		t.Errorf("top history query = %v; want kind:video", first["query"])
	}
}

// TestQueryTransportDown: an unreachable transport yields the actionable
// "is the agent daemon running?" message.
func TestQueryTransportDown(t *testing.T) {
	_, _, err := runQueryCmd(t, "--socket", deadSocketPath(t), "kind:video")
	if err == nil {
		t.Fatal("expected a transport error against a dead socket")
	}
	if !strings.Contains(err.Error(), "is the agent daemon running") {
		t.Fatalf("error = %v; want the actionable daemon hint", err)
	}
}

// TestQueryUnsupportedFilter: a parseable-but-unrunnable filter (tag:) surfaces
// the local-index boundary explanation end-to-end through the real engine.
func TestQueryUnsupportedFilter(t *testing.T) {
	path := serveLocalTransport(t, realIndexHandler(t))
	_, _, err := runQueryCmd(t, "--socket", path, "tag:favorite")
	if err == nil {
		t.Fatal("expected an unsupported_filter error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "unsupported") || !strings.Contains(msg, "central search") {
		t.Fatalf("error = %v; want the local-index boundary explanation", err)
	}
}
