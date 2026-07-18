package localapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
)

// TestHistoryNeverTouchesOutbox is the P7-T6 privacy invariant (the acceptance
// test that matters): after heavy simulated use with query recording ON, the
// outbox table — the ONLY thing that ships to central — contains ZERO rows
// attributable to search history. History lives in a physically separate database
// file, so it is architecturally incapable of riding replication.
func TestHistoryNeverTouchesOutbox(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	indexPath := seedIndex(t, now)

	// A separate history DB file — the isolation guarantee.
	histPath := filepath.Join(t.TempDir(), "history.db")
	if histPath == indexPath {
		t.Fatal("history and index must be different files")
	}
	hist, err := history.Open(histPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hist.Close() })

	searcher, err := query.NewSearcher(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })

	srv := New(Config{
		Path:     "test",
		Searcher: searcher,
		History:  hist,
		Policy:   func() PolicyView { return PolicyView{LocalAccessEnabled: true} },
	})
	handler := srv.Handler()

	// Baseline outbox count in the INDEX database (seedIndex writes items only, no
	// outbox rows — but assert the table exists and is the thing we're guarding).
	idxStore, err := index.Open(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { idxStore.Close() })
	baseline := outboxCount(t, idxStore)

	// Hammer a variety of queries with recording on.
	ctx := context.Background()
	queries := []string{
		"kind:video", "ext:flac", "Arcane", "kind:audio size:>1M",
		"notes", "Movies", "kind:document", "song",
	}
	const rounds = 250
	for i := 0; i < rounds; i++ {
		q := queries[i%len(queries)]
		postQuery(t, handler, q)
		_ = ctx
	}

	// THE INVARIANT: the outbox is untouched by all that recording.
	if got := outboxCount(t, idxStore); got != baseline {
		t.Fatalf("outbox row count changed from %d to %d — search history LEAKED onto the replication path", baseline, got)
	}
	if got := outboxCount(t, idxStore); got != 0 {
		t.Fatalf("outbox expected 0 rows, got %d", got)
	}

	// And prove recording actually happened (else the test is vacuous).
	top, err := hist.Top(ctx, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(top) != len(queries) {
		t.Fatalf("expected %d distinct history entries, got %d", len(queries), len(top))
	}
	var totalHits float64
	for _, e := range top {
		totalHits += e.Hits
	}
	if int(totalHits) != rounds {
		t.Fatalf("expected %d total recorded hits, got %v", rounds, totalHits)
	}
}

// TestHistoryEndpointServesFrecency exercises GET /v1/history end-to-end.
func TestHistoryEndpointServesFrecency(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	indexPath := seedIndex(t, now)
	hist, err := history.Open(filepath.Join(t.TempDir(), "history.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hist.Close() })
	searcher, err := query.NewSearcher(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })

	srv := New(Config{Path: "test", Searcher: searcher, History: hist,
		Policy: func() PolicyView { return PolicyView{LocalAccessEnabled: true} }})
	handler := srv.Handler()

	// Record "Arcane" more than "notes".
	for i := 0; i < 3; i++ {
		postQuery(t, handler, "Arcane")
	}
	postQuery(t, handler, "notes")

	req := httptest.NewRequest(http.MethodGet, "/v1/history?limit=10", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("history status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var resp HistoryResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Entries) != 2 {
		t.Fatalf("want 2 entries, got %d", len(resp.Entries))
	}
	if resp.Entries[0].Query != "Arcane" {
		t.Fatalf("expected 'Arcane' first (higher frecency), got %+v", resp.Entries)
	}
}

// TestHistoryDisabledEmptyEndpoint: with no history store the endpoint returns an
// empty list (not a 404/500) and query recording is a no-op.
func TestHistoryDisabledEmptyEndpoint(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	indexPath := seedIndex(t, now)
	searcher, err := query.NewSearcher(indexPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })
	srv := New(Config{Path: "test", Searcher: searcher, // no History
		Policy: func() PolicyView { return PolicyView{LocalAccessEnabled: true} }})
	handler := srv.Handler()

	postQuery(t, handler, "kind:video") // must not panic without a recorder

	req := httptest.NewRequest(http.MethodGet, "/v1/history", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("history status = %d", rec.Code)
	}
	var resp HistoryResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Entries) != 0 {
		t.Fatalf("want empty entries with history disabled, got %+v", resp.Entries)
	}
}

func postQuery(t *testing.T, handler http.Handler, q string) {
	t.Helper()
	body, _ := json.Marshal(QueryRequest{Query: q, Limit: 50})
	req := httptest.NewRequest(http.MethodPost, "/v1/query", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("query %q status = %d, body=%s", q, rec.Code, rec.Body.String())
	}
}

func outboxCount(t *testing.T, st *index.Store) int {
	t.Helper()
	var n int
	if err := st.DB().QueryRowContext(context.Background(),
		`SELECT COUNT(*) FROM outbox`).Scan(&n); err != nil {
		t.Fatal(fmt.Errorf("count outbox: %w", err))
	}
	return n
}
