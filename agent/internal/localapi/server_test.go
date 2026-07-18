package localapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
)

// seedIndex builds a temp local index with a small fixed corpus and returns its
// path. now anchors mtime so time filters are deterministic.
func seedIndex(t *testing.T, now time.Time) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "index.db")
	st, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	tx, _ := st.Begin(ctx)
	rid, err := index.EnsureRoot(ctx, tx, "/media")
	if err != nil {
		t.Fatal(err)
	}
	tx.Commit()

	type row struct {
		rel, ext, media, qhash string
		size                   int64
		ageDays                int
		sidecar                bool
	}
	rows := []row{
		{rel: "Movies/Arcane.S01E01.mkv", ext: "mkv", media: "video", size: 2 * 1024 * 1024 * 1024, ageDays: 1, qhash: "aa11"},
		{rel: "Movies/Arcane.S01E01.nfo", ext: "nfo", media: "other", size: 2048, ageDays: 1, sidecar: true},
		{rel: "Music/Song.flac", ext: "flac", media: "audio", size: 40 * 1024 * 1024, ageDays: 40},
		{rel: "Docs/notes.txt", ext: "txt", media: "document", size: 100, ageDays: 100},
	}
	for _, r := range rows {
		tx, _ := st.Begin(ctx)
		id, _ := index.NewID()
		ts := now.Add(-time.Duration(r.ageDays) * 24 * time.Hour)
		it := &index.Item{
			ID: id, RootID: rid, RelPath: r.rel, Filename: filepath.Base(r.rel),
			Extension: r.ext, Size: r.size, MtimeNs: ts.UnixNano(),
			QuickHash: r.qhash, MediaType: r.media,
			Status: index.StatusActive, IsSidecar: r.sidecar, FirstSeen: ts, LastSeen: ts,
		}
		if err := index.InsertItem(ctx, tx, it); err != nil {
			t.Fatal(err)
		}
		tx.Commit()
	}
	st.Close()
	return path
}

// testServer wires a Server over a seeded index, returning it plus a live
// item-count function. policy defaults to enabled.
func testServer(t *testing.T, policy func() PolicyView) *Server {
	t.Helper()
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	path := seedIndex(t, now)

	searcher, err := query.NewSearcher(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })

	countStore, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { countStore.Close() })

	return New(Config{
		Searcher: searcher,
		Count: func(ctx context.Context) (int, error) {
			var n int
			err := countStore.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM items WHERE status='active'`).Scan(&n)
			return n, err
		},
		Policy: policy,
		Now:    func() time.Time { return now },
	})
}

func doQuery(t *testing.T, h http.Handler, req QueryRequest) (*http.Response, []byte) {
	t.Helper()
	body, _ := json.Marshal(req)
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	resp, err := http.Post(srv.URL+"/v1/query", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	buf.ReadFrom(resp.Body)
	return resp, buf.Bytes()
}

func TestQueryContractRoundTrip(t *testing.T) {
	s := testServer(t, nil)
	resp, raw := doQuery(t, s.Handler(), QueryRequest{Query: "kind:video size:>1G", Limit: 10})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", resp.StatusCode, raw)
	}
	var qr QueryResponse
	if err := json.Unmarshal(raw, &qr); err != nil {
		t.Fatalf("decode: %v (body=%s)", err, raw)
	}
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("unexpected rows: %+v", qr.Rows)
	}
	r := qr.Rows[0]
	if r.Kind == nil || *r.Kind != "video" {
		t.Errorf("kind = %v, want video", r.Kind)
	}
	if r.Size != 2*1024*1024*1024 {
		t.Errorf("size = %d", r.Size)
	}
	if r.QuickHash == nil || *r.QuickHash != "aa11" {
		t.Errorf("quick_hash = %v", r.QuickHash)
	}
	if r.FuzzyMatched {
		t.Errorf("exact hit must not be fuzzy")
	}
	// Scope is always present and inactive with an empty (non-null) predicate list.
	if qr.Scope.Active || qr.Scope.Predicates == nil {
		t.Errorf("scope = %+v; want inactive with non-nil predicates", qr.Scope)
	}

	// Contract key check: the JSON carries snake_case keys and no unknown keys.
	var m map[string]json.RawMessage
	json.Unmarshal(raw, &m)
	for _, k := range []string{"rows", "total", "truncated", "fuzzy", "scope", "elapsed_ms", "notice"} {
		if _, ok := m[k]; !ok {
			t.Errorf("response missing contract key %q", k)
		}
	}
	if len(m) != 7 {
		t.Errorf("response has %d top-level keys, want 7 (drift from contract)", len(m))
	}
}

func TestSidecarsExcludedByDefault(t *testing.T) {
	s := testServer(t, nil)
	_, raw := doQuery(t, s.Handler(), QueryRequest{Query: "arcane"})
	var qr QueryResponse
	json.Unmarshal(raw, &qr)
	if len(qr.Rows) != 1 {
		t.Fatalf("sidecars must be excluded: got %v", qr.Rows)
	}
}

func TestFuzzyNoticeSet(t *testing.T) {
	s := testServer(t, nil)
	// "arcaen" (transposition) → zero exact hits → fuzzy re-rank fires.
	_, raw := doQuery(t, s.Handler(), QueryRequest{Query: "arcaen"})
	var qr QueryResponse
	json.Unmarshal(raw, &qr)
	if !qr.Fuzzy || qr.Notice == nil {
		t.Fatalf("fuzzy query must set fuzzy=true and a notice: %+v", qr)
	}
	if len(qr.Rows) == 0 || !qr.Rows[0].FuzzyMatched || qr.Rows[0].Score == nil {
		t.Fatalf("fuzzy rows must be flagged + scored: %+v", qr.Rows)
	}
}

func TestLimitClamp(t *testing.T) {
	s := testServer(t, nil)
	// limit above the 1000 ceiling is clamped; a huge limit must not error.
	resp, raw := doQuery(t, s.Handler(), QueryRequest{Query: "kind:audio", Limit: 999999})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", resp.StatusCode, raw)
	}
}

func TestParseErrorIs400(t *testing.T) {
	s := testServer(t, nil)
	resp, raw := doQuery(t, s.Handler(), QueryRequest{Query: "size:1X"})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", resp.StatusCode, raw)
	}
	var eb errorBody
	json.Unmarshal(raw, &eb)
	if eb.Code != "bad_size_suffix" || eb.Position == nil {
		t.Fatalf("expected bad_size_suffix with position: %+v", eb)
	}
}

func TestExecErrorIs422(t *testing.T) {
	s := testServer(t, nil)
	resp, raw := doQuery(t, s.Handler(), QueryRequest{Query: "tag:favorite"})
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d body=%s", resp.StatusCode, raw)
	}
	var eb errorBody
	json.Unmarshal(raw, &eb)
	if eb.Code != query.ErrUnsupportedFilter || len(eb.Keys) == 0 {
		t.Fatalf("expected unsupported_filter with keys: %+v", eb)
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	s := testServer(t, nil)
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	resp, err := http.Post(srv.URL+"/v1/query", "application/json", strings.NewReader(`{"query":"x","bogus":1}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("unknown field must be rejected (extra=forbid parity): status=%d", resp.StatusCode)
	}
}

func TestHealth(t *testing.T) {
	s := testServer(t, func() PolicyView {
		return PolicyView{LocalAccessEnabled: true, WebUIEnabled: true, AuthRequired: true, HasVersion: true, Version: 7}
	})
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	resp, err := http.Get(srv.URL + "/v1/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var h HealthResponse
	json.NewDecoder(resp.Body).Decode(&h)
	if !h.ReadOnly {
		t.Error("read_only must always be true")
	}
	if !h.IndexReady || h.ItemCount != 4 {
		t.Errorf("index_ready/item_count wrong: %+v", h)
	}
	if h.PolicyVersion == nil || *h.PolicyVersion != 7 {
		t.Errorf("policy_version = %v", h.PolicyVersion)
	}
	if !h.WebUIEnabled || !h.AuthRequired {
		t.Errorf("policy flags not reflected: %+v", h)
	}
	if h.Status != "ok" {
		t.Errorf("status = %q", h.Status)
	}
}

func TestQueryRefusedWhenDisabled(t *testing.T) {
	s := testServer(t, func() PolicyView { return PolicyView{LocalAccessEnabled: false} })
	resp, _ := doQuery(t, s.Handler(), QueryRequest{Query: "arcane"})
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("disabled policy must refuse queries: status=%d", resp.StatusCode)
	}
}

func TestMethodBackstop(t *testing.T) {
	s := testServer(t, nil)
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/v1/query", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("non-GET/POST must be 405: got %d", resp.StatusCode)
	}
}

func TestPolicyViewFromRaw(t *testing.T) {
	// Absent keys default to the fail-safe posture: CLI enabled, web UI off.
	pv := PolicyViewFromRaw([]byte(`{}`), 3, true)
	if !pv.LocalAccessEnabled || pv.WebUIEnabled || pv.AuthRequired {
		t.Fatalf("defaults wrong: %+v", pv)
	}
	if !pv.HasVersion || pv.Version != 3 {
		t.Fatalf("version not carried: %+v", pv)
	}
	// Explicit disable is honored.
	pv = PolicyViewFromRaw([]byte(`{"local_access_enabled":false,"web_ui_enabled":true,"auth_required":true}`), 0, false)
	if pv.LocalAccessEnabled || !pv.WebUIEnabled || !pv.AuthRequired {
		t.Fatalf("explicit keys not honored: %+v", pv)
	}
	// A nil/empty body keeps the fail-safe default (enabled).
	if pv := PolicyViewFromRaw(nil, 0, false); !pv.LocalAccessEnabled {
		t.Fatalf("nil body must default enabled: %+v", pv)
	}
}
