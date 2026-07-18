package reconcile

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// mockReconcile is an in-memory central implementing the FROZEN reconcile
// protocol (start/rows/finish). Behaviors are toggled per-test.
type mockReconcile struct {
	mu sync.Mutex

	matchDigest    string // start digest == this -> {"status":"match"}
	maxRowsPerPage int    // a rows page larger than this -> 413 (0 = unlimited)
	expireRows     int    // return 404 on the next N rows calls (session expiry)
	mismatchFinish int    // return 409 digest_mismatch on the next N finish calls

	// captured
	startCalls, rowsCalls, finishCalls int
	lastRebuilt, lastResetSeq          bool
	receivedRows                       int
	maxPageSeen                        int
	sessions                           map[string]bool
	sessionSeq                         int
}

func newMockReconcile() *mockReconcile { return &mockReconcile{sessions: map[string]bool{}} }

func (m *mockReconcile) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		p := r.URL.Path
		switch {
		case strings.HasSuffix(p, "/reconcile/start"):
			m.handleStart(w, r)
		case strings.HasSuffix(p, "/rows"):
			m.handleRows(w, r)
		case strings.HasSuffix(p, "/finish"):
			m.handleFinish(w, r)
		default:
			http.Error(w, `{"detail":"not found"}`, http.StatusNotFound)
		}
	})
}

func (m *mockReconcile) handleStart(w http.ResponseWriter, r *http.Request) {
	m.startCalls++
	var req struct {
		LibraryRef string `json:"library_ref"`
		Digest     string `json:"digest"`
		RowCount   int    `json:"row_count"`
		Rebuilt    bool   `json:"rebuilt"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	m.lastRebuilt = req.Rebuilt
	writeJSON := func(v any) { w.Header().Set("Content-Type", "application/json"); _ = json.NewEncoder(w).Encode(v) }
	if req.Digest == m.matchDigest {
		writeJSON(map[string]any{"status": "match"})
		return
	}
	m.sessionSeq++
	sid := "sess-" + itoa(m.sessionSeq)
	m.sessions[sid] = true
	writeJSON(map[string]any{"status": "mismatch", "session_id": sid})
}

func (m *mockReconcile) handleRows(w http.ResponseWriter, r *http.Request) {
	m.rowsCalls++
	if m.expireRows > 0 {
		m.expireRows--
		http.Error(w, `{"detail":"session expired"}`, http.StatusNotFound)
		return
	}
	var req struct {
		Rows []json.RawMessage `json:"rows"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if len(req.Rows) > m.maxPageSeen {
		m.maxPageSeen = len(req.Rows)
	}
	if m.maxRowsPerPage > 0 && len(req.Rows) > m.maxRowsPerPage {
		http.Error(w, `{"detail":"page too large"}`, http.StatusRequestEntityTooLarge)
		return
	}
	m.receivedRows += len(req.Rows)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func (m *mockReconcile) handleFinish(w http.ResponseWriter, r *http.Request) {
	m.finishCalls++
	var req struct {
		Digest   string `json:"digest"`
		RowCount int    `json:"row_count"`
		ResetSeq bool   `json:"reset_seq"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	m.lastResetSeq = req.ResetSeq
	if m.mismatchFinish > 0 {
		m.mismatchFinish--
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]any{"reason": "digest_mismatch"})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status": "reconciled", "matched": req.RowCount, "missing_on_central": 0, "extra_on_central": 2,
	})
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [12]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

// --- helpers ---------------------------------------------------------------

func newTestSweeper(t *testing.T, st storeAndOutbox, srv *httptest.Server, pageSize int) *Sweeper {
	t.Helper()
	client := NewClient(ClientConfig{
		BaseURL:  srv.URL,
		AgentID:  "agent-1",
		HTTP:     srv.Client(),
		PageSize: pageSize,
	})
	return NewSweeper(st.store, st.ob, client, nil)
}

type storeAndOutbox struct {
	store *index.Store
	ob    *outbox.Outbox
}

// openReconciledStore opens a fresh store and clears the durable rebuilt marker
// that index.Open writes on fresh-create, giving a baseline "already reconciled"
// agent — the correct starting point for tests that exercise the normal (non-
// rebuilt) protocol path.
func openReconciledStore(t *testing.T) *index.Store {
	t.Helper()
	st := openStore(t)
	if err := st.ClearRebuiltPending(context.Background()); err != nil {
		t.Fatal(err)
	}
	return st
}

func rebuiltPending(t *testing.T, st *index.Store) bool {
	t.Helper()
	p, err := st.RebuiltPending(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func seedOutbox(t *testing.T, st *index.Store, n int) {
	t.Helper()
	ctx := context.Background()
	for i := 0; i < n; i++ {
		tx, err := st.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := outbox.Write(ctx, tx, outbox.Event{
			ItemID: "i" + itoa(i), Op: outbox.OpCreated, LibraryRef: "/media",
			RelPath: "f" + itoa(i) + ".mkv", Size: int64(i), MtimeNs: int64(i) * 1e9,
		}); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatal(err)
		}
	}
}

func manifestDigest(t *testing.T, st *index.Store, rootID string) string {
	t.Helper()
	items, err := st.ActiveItems(context.Background(), rootID)
	if err != nil {
		t.Fatal(err)
	}
	return Digest(ProjectItems(items))
}

// --- tests -----------------------------------------------------------------

func TestSweepMatchPathDoesNothing(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	seedItem(t, st, rid, "b.mkv", "active", false)

	mock := newMockReconcile()
	mock.matchDigest = manifestDigest(t, st, rid)
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Roots) != 1 || !res.Roots[0].Matched {
		t.Fatalf("digest match should end at start with Matched=true: %+v", res.Roots)
	}
	if mock.rowsCalls != 0 || mock.finishCalls != 0 {
		t.Errorf("match path must not stream rows or finish (rows=%d finish=%d)", mock.rowsCalls, mock.finishCalls)
	}
}

func TestSweepMismatchPagesAndPassesCounters(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	for i := 0; i < 5; i++ {
		seedItem(t, st, rid, "f"+itoa(i)+".mkv", "active", false)
	}
	mock := newMockReconcile() // matchDigest empty -> always mismatch
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 2) // 5 rows / page 2 -> 3 pages
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	rr := res.Roots[0]
	if rr.Matched || rr.Err != nil {
		t.Fatalf("expected reconcile, got %+v", rr)
	}
	if mock.receivedRows != 5 {
		t.Errorf("central should receive all 5 rows, got %d", mock.receivedRows)
	}
	if mock.rowsCalls != 3 {
		t.Errorf("5 rows at page 2 => 3 pages, got %d rows calls", mock.rowsCalls)
	}
	if got, ok := rr.Finish.Counters["extra_on_central"]; !ok || toInt(got) != 2 {
		t.Errorf("finish counters must pass through, got %+v", rr.Finish.Counters)
	}
	if toInt(rr.Finish.Counters["matched"]) != 5 {
		t.Errorf("matched counter = %v, want 5", rr.Finish.Counters["matched"])
	}
}

func TestSweep413HalvesPageSize(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	for i := 0; i < 3; i++ {
		seedItem(t, st, rid, "f"+itoa(i)+".mkv", "active", false)
	}
	mock := newMockReconcile()
	mock.maxRowsPerPage = 1 // any page >1 row -> 413
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 4) // starts too big
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Roots[0].Err != nil {
		t.Fatalf("413 halving should recover, got %v", res.Roots[0].Err)
	}
	if mock.receivedRows != 3 {
		t.Errorf("all 3 rows must arrive after halving, got %d", mock.receivedRows)
	}
	if mock.maxPageSeen > 1 {
		// The oversized pages were rejected; only accepted pages count receivedRows,
		// but maxPageSeen records the largest attempted — halving must reach 1.
		t.Logf("largest page attempted: %d (rejected until <=1)", mock.maxPageSeen)
	}
}

func TestSweep404RestartsOnce(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.expireRows = 1 // first rows call 404s; the restart's rows call succeeds
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Roots[0].Err != nil {
		t.Fatalf("a single session expiry must restart-and-succeed, got %v", res.Roots[0].Err)
	}
	if mock.startCalls != 2 {
		t.Errorf("expected exactly one restart (2 starts), got %d", mock.startCalls)
	}
}

func TestSweep404TwiceSurfacesError(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.expireRows = 2 // both the initial and the restart expire
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err == nil || res.Roots[0].Err == nil {
		t.Fatal("a second expiry must surface as a per-root error")
	}
	if mock.startCalls != 2 {
		t.Errorf("restart-once means exactly 2 starts, got %d", mock.startCalls)
	}
}

func TestSweepDigestMismatchRestartsOnceThenErrors(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.mismatchFinish = 2 // both finishes 409 digest_mismatch
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err == nil || res.Roots[0].Err == nil {
		t.Fatal("a persistent digest mismatch must surface after the single restart")
	}
	if mock.finishCalls != 2 {
		t.Errorf("restart-once => 2 finish attempts, got %d", mock.finishCalls)
	}
}

func TestSweepDigestMismatchRecoversOnRestart(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.mismatchFinish = 1 // first finish 409, restart's finish succeeds
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil || res.Roots[0].Err != nil {
		t.Fatalf("one mismatch then success must reconcile, got %v / %v", err, res.Roots[0].Err)
	}
	if mock.finishCalls != 2 {
		t.Errorf("expected 2 finish calls (one restart), got %d", mock.finishCalls)
	}
}

func TestSweepForceResetMarksOutboxSent(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	seedOutbox(t, st, 4) // an unsent backlog the reset must supersede

	mock := newMockReconcile() // mismatch -> full path incl. finish(reset_seq)
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	ob := outbox.New(st.DB())
	sw := newTestSweeper(t, storeAndOutbox{st, ob}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{ForceReset: true})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Reset || res.OutboxMarked != 4 {
		t.Errorf("force reset must supersede all 4 outbox rows, got reset=%v marked=%d", res.Reset, res.OutboxMarked)
	}
	if n, _ := ob.CountUnsent(context.Background()); n != 0 {
		t.Errorf("outbox backlog must be fully marked sent, %d unsent", n)
	}
	if !mock.lastResetSeq {
		t.Error("finish must carry reset_seq=true on a forced reset")
	}
}

func TestSweepRebuiltSignalSetsRebuiltAndResets(t *testing.T) {
	st := openStore(t)
	st.Rebuilt = true // simulate a corruption rebuild this process
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	seedOutbox(t, st, 2)

	mock := newMockReconcile()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	ob := outbox.New(st.DB())
	sw := newTestSweeper(t, storeAndOutbox{st, ob}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !mock.lastRebuilt {
		t.Error("start must carry rebuilt=true when the index was rebuilt")
	}
	if !res.Reset || res.OutboxMarked != 2 {
		t.Errorf("a rebuilt sweep resets and supersedes the outbox, got reset=%v marked=%d", res.Reset, res.OutboxMarked)
	}
}

func TestRebuiltSignalOutboxEmptyFallback(t *testing.T) {
	st := openReconciledStore(t)
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false) // items exist, outbox never written
	mock := newMockReconcile()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Rebuilt || !mock.lastRebuilt {
		t.Error("empty outbox + present items must be treated as a rebuilt signal")
	}
}

// TestDurableMarkerDrivesRebuiltAcrossProcessBoundary is the E2E-gap regression at
// the sweep layer: a store whose durable marker is set (as index.Open leaves it
// after a fresh-create/rebuild) makes the sweep send rebuilt=true even though
// Store.Rebuilt is false (a different process reopened a clean file) and the
// outbox is NOT empty (a prior scan refilled it) — the exact conditions the old
// empty-outbox fallback could not detect.
func TestDurableMarkerDrivesRebuiltAcrossProcessBoundary(t *testing.T) {
	st := openStore(t) // fresh create -> durable marker set
	st.Rebuilt = false // as if a SEPARATE process reopened the clean file
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	seedOutbox(t, st, 3) // a prior scan already refilled the outbox (fallback blind)
	if !rebuiltPending(t, st) {
		t.Fatal("precondition: fresh create must leave the durable marker set")
	}

	mock := newMockReconcile()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !mock.lastRebuilt {
		t.Error("durable marker must force rebuilt=true even with Rebuilt=false and a non-empty outbox")
	}
	if !mock.lastResetSeq {
		t.Error("finish must carry reset_seq=true so central resets its watermark")
	}
	if res.OutboxMarked != 3 {
		t.Errorf("the reset must supersede the refilled outbox, marked=%d want 3", res.OutboxMarked)
	}
}

func TestMarkerClearedOnSuccessfulReconcile(t *testing.T) {
	st := openStore(t) // marker set
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile() // mismatch -> finish success
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	res, err := sw.Sweep(context.Background(), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Rebuilt || !mock.lastRebuilt {
		t.Fatal("sweep must have carried rebuilt=true")
	}
	if rebuiltPending(t, st) {
		t.Error("a successful rebuilt-carrying sweep must clear the durable marker")
	}
}

func TestMarkerClearedOnRebuiltMatch(t *testing.T) {
	st := openStore(t) // marker set
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.matchDigest = manifestDigest(t, st, rid) // start -> match (no finish)
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	if _, err := sw.Sweep(context.Background(), Options{}); err != nil {
		t.Fatal(err)
	}
	if !mock.lastRebuilt {
		t.Fatal("start must carry rebuilt=true")
	}
	if rebuiltPending(t, st) {
		t.Error("a rebuilt-carrying start→match must clear the durable marker")
	}
}

func TestMarkerPersistsOnFailedSweep(t *testing.T) {
	st := openStore(t) // marker set
	rid := seedRoot(t, st, "/media")
	seedItem(t, st, rid, "a.mkv", "active", false)
	mock := newMockReconcile()
	mock.expireRows = 2 // both the initial and the restart 404 -> sweep fails
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	sw := newTestSweeper(t, storeAndOutbox{st, outbox.New(st.DB())}, srv, 0)
	if _, err := sw.Sweep(context.Background(), Options{}); err == nil {
		t.Fatal("the sweep must fail (both session attempts expire)")
	}
	if !rebuiltPending(t, st) {
		t.Error("a FAILED sweep must PRESERVE the durable marker for the next attempt")
	}
}

func toInt(v any) int {
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return -1
}
