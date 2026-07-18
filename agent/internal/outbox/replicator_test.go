package outbox

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// mockCentral is an in-memory port of the central replication endpoint. Its
// verdict logic mirrors backend/filearr/agentsync.py check_batch: it accepts ONLY
// a contiguous continuation from last_seq+1, and answers any gap/stale/duplicate
// with 409 {reason, expected_seq_no = last_seq+1}. It records every applied seq so
// a test can prove NO double-apply across retries.
type mockCentral struct {
	mu           sync.Mutex
	lastSeq      int64
	applied      map[int64]int // seq_no -> times applied (must stay ≤1)
	batches      int
	upserted     int
	tombstoned   int
	doubleApply  bool
	requestCount int

	// down, when true, makes the handler return 503 (offline simulation).
	down bool
	// notFound, when true, returns 404 (feature-off simulation).
	notFound bool
}

func newMockCentral() *mockCentral { return &mockCentral{applied: map[int64]int{}} }

func (m *mockCentral) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.requestCount++
		if m.down {
			http.Error(w, `{"detail":"unavailable"}`, http.StatusServiceUnavailable)
			return
		}
		if m.notFound {
			http.Error(w, `{"detail":"replication disabled"}`, http.StatusNotFound)
			return
		}
		var batch struct {
			AgentID string        `json:"agent_id"`
			Entries []wireForTest `json:"entries"`
		}
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			http.Error(w, `{"detail":"bad json"}`, http.StatusBadRequest)
			return
		}
		reason, ok := m.checkBatch(batch.Entries)
		if !ok {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"reason": reason, "expected_seq_no": m.lastSeq + 1,
			})
			return
		}
		// Accept: apply each entry exactly once, advance the contiguous frontier.
		up, tomb := 0, 0
		for _, e := range batch.Entries {
			m.applied[e.SeqNo]++
			if m.applied[e.SeqNo] > 1 {
				m.doubleApply = true
			}
			if e.EventType == "deleted" {
				tomb++
			} else {
				up++
			}
			m.lastSeq = e.SeqNo
		}
		m.batches++
		m.upserted += up
		m.tombstoned += tomb
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"applied": len(batch.Entries), "upserted": up, "tombstoned": tomb,
			"noop_tombstones": 0, "libraries_created": 0, "last_seq": m.lastSeq,
		})
	})
}

// checkBatch is the minimal port of agentsync.check_batch's accept rule.
func (m *mockCentral) checkBatch(entries []wireForTest) (reason string, ok bool) {
	if len(entries) == 0 {
		return "empty", false
	}
	first := entries[0].SeqNo
	if first <= m.lastSeq {
		return "stale", false // already-applied replay (duplicate)
	}
	if first != m.lastSeq+1 {
		return "gap", false
	}
	prev := first
	for _, e := range entries[1:] {
		if e.SeqNo != prev+1 {
			return "internal_gap", false
		}
		prev = e.SeqNo
	}
	return "", true
}

func (m *mockCentral) snapshot() (lastSeq int64, batches int, double bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lastSeq, m.batches, m.doubleApply
}

// --- helpers ---------------------------------------------------------------

func seedEvents(t *testing.T, st *index.Store, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		writeOne(t, st, Event{
			ItemID: fmt.Sprintf("i%d", i), Op: OpCreated, LibraryRef: "/m",
			RelPath: fmt.Sprintf("f%d.mkv", i), Size: int64(i), MtimeNs: int64(i) * 1e9,
		})
	}
}

func newTestReplicator(t *testing.T, st *index.Store, srv *httptest.Server, cfg Config) *Replicator {
	t.Helper()
	cfg.BaseURL = srv.URL
	cfg.AgentID = "agent-1"
	cfg.HTTP = srv.Client()
	if cfg.AuthFn == nil {
		cfg.AuthFn = func() string { return "fp-abc" }
	}
	return NewReplicator(New(st.DB()), cfg)
}

func countUnsent(t *testing.T, st *index.Store) int {
	t.Helper()
	n, err := New(st.DB()).CountUnsent(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// --- tests -----------------------------------------------------------------

func TestPushDrainsBacklogAcrossBatches(t *testing.T) {
	// 72h-catch-up analogue: a large backlog drains in maxRows-sized batches, all
	// marked, contiguous, with no double-apply.
	st := openStore(t)
	const total = 5000
	seedEvents(t, st, total)
	mock := newMockCentral()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{MaxRows: 500})

	c, err := rep.Push(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if c.Batches != total/500 {
		t.Errorf("batches = %d, want %d", c.Batches, total/500)
	}
	if c.Rows != total {
		t.Errorf("rows marked = %d, want %d", c.Rows, total)
	}
	if n := countUnsent(t, st); n != 0 {
		t.Errorf("unsent after drain = %d, want 0", n)
	}
	last, batches, double := mock.snapshot()
	if last != total || double || batches != total/500 {
		t.Errorf("central: last_seq=%d batches=%d double=%v", last, batches, double)
	}
}

func TestPushEmitsAuthBearer(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 1)
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewEncoder(w).Encode(map[string]any{"applied": 1, "last_seq": 1})
	}))
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{AuthFn: func() string { return "fp-xyz" }})
	if _, err := rep.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	if gotAuth != "Bearer fp-xyz" {
		t.Errorf("Authorization = %q, want 'Bearer fp-xyz'", gotAuth)
	}
}

// killOnceTransport forwards the first request to the server (so it applies)
// then drops the response and returns an error — simulating the agent dying
// AFTER central committed but BEFORE it marked the batch sent.
type killOnceTransport struct {
	inner  http.RoundTripper
	killed bool
}

func (k *killOnceTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := k.inner.RoundTrip(req)
	if !k.killed {
		k.killed = true
		if resp != nil {
			resp.Body.Close()
		}
		return nil, fmt.Errorf("simulated agent death after server apply")
	}
	return resp, err
}

func TestKillMidBatchNoLossNoDoubleApply(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 10)
	mock := newMockCentral()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()

	// Phase 1: the batch reaches central (applied) but the agent "dies" before
	// marking sent — flush returns an error, rows stay unsent.
	dyingClient := &http.Client{Transport: &killOnceTransport{inner: srv.Client().Transport}}
	rep1 := NewReplicator(New(st.DB()), Config{BaseURL: srv.URL, AgentID: "agent-1", HTTP: dyingClient, MaxRows: 10})
	if _, err := rep1.Push(context.Background()); err == nil {
		t.Fatal("phase 1 must surface the simulated death as an error")
	}
	if n := countUnsent(t, st); n != 10 {
		t.Fatalf("after death, all 10 rows must remain unsent (durable), got %d", n)
	}
	if last, _, _ := mock.snapshot(); last != 10 {
		t.Fatalf("central should have applied the batch in phase 1, last_seq=%d", last)
	}

	// Phase 2 (restart): resend. Central sees a duplicate → 409 expected=11 →
	// agent fast-forwards, marking all 10 sent. Zero loss, zero double-apply.
	rep2 := newTestReplicator(t, st, srv, Config{MaxRows: 10})
	if _, err := rep2.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := countUnsent(t, st); n != 0 {
		t.Errorf("after resend, 0 rows should remain unsent, got %d", n)
	}
	_, _, double := mock.snapshot()
	if double {
		t.Error("central applied a seq twice — replay was NOT idempotent")
	}
}

func TestFullRewindResendsLostRows(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 10)
	ob := New(st.DB())
	// Simulate 1..5 previously acked-and-marked; unsent frontier is now 6.
	if _, err := ob.MarkSent(context.Background(), 1, 5, "old"); err != nil {
		t.Fatal(err)
	}
	// Central, however, only committed up to 4 (it lost 5): last_seq=4.
	mock := newMockCentral()
	mock.lastSeq = 4
	for s := int64(1); s <= 4; s++ {
		mock.applied[s] = 1
	}
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{MaxRows: 10})

	// First flush sends [6..10] → gap (expected 5) → rewind re-exposes 5 → resend
	// [5..10] accepted. Push loops through both internally.
	if _, err := rep.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := countUnsent(t, st); n != 0 {
		t.Errorf("rewind+resend should fully drain, %d unsent", n)
	}
	last, _, double := mock.snapshot()
	if last != 10 {
		t.Errorf("central last_seq = %d, want 10", last)
	}
	if double {
		t.Error("rewind double-applied a seq")
	}
	if mock.applied[5] != 1 {
		t.Errorf("the lost row (seq 5) must be re-applied exactly once, got %d", mock.applied[5])
	}
}

func TestUnrecoverableGapBacksOffNotHotLoops(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 3) // unsent 1..3
	// Central demands seq 0 (below our frontier, nothing to rewind) forever.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]any{"reason": "gap", "expected_seq_no": 0})
	}))
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{MaxRows: 10})
	// Push must return an error (not spin) because the cursor cannot move.
	if _, err := rep.Push(context.Background()); err == nil {
		t.Fatal("an unrecoverable gap must surface as an error, not a hot loop")
	}
}

func TestOfflineBlocksButDoesNotDrop(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 5)
	mock := newMockCentral()
	mock.down = true
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 5, MaxAge: 5 * time.Millisecond, Poll: 5 * time.Millisecond,
		Backoff: BackoffConfig{Min: 5 * time.Millisecond, Max: 20 * time.Millisecond},
	})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	// While central is down, rows must accumulate unsent — never dropped.
	time.Sleep(80 * time.Millisecond)
	if n := countUnsent(t, st); n != 5 {
		t.Errorf("offline: all 5 rows must stay unsent, got %d", n)
	}

	// Bring central up; the drain must catch up without intervention.
	mock.mu.Lock()
	mock.down = false
	mock.mu.Unlock()
	waitFor(t, func() bool { return countUnsent(t, st) == 0 }, time.Second)

	cancel()
	<-done
	if _, _, double := mock.snapshot(); double {
		t.Error("recovery double-applied")
	}
}

func TestRunCountTriggerFlushesBeforeAge(t *testing.T) {
	st := openStore(t)
	mock := newMockCentral()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	// Huge age window: only the row-count trigger can fire within the test.
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 3, MaxAge: time.Hour, Poll: 5 * time.Millisecond,
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	seedEvents(t, st, 3)
	waitFor(t, func() bool { return countUnsent(t, st) == 0 }, time.Second)
	cancel()
	<-done
}

func TestRunAgeTriggerFlushesSmallBatch(t *testing.T) {
	st := openStore(t)
	mock := newMockCentral()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	// Huge row cap: a single row can only leave via the AGE trigger.
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 10000, MaxAge: 20 * time.Millisecond, Poll: 5 * time.Millisecond,
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	seedEvents(t, st, 1)
	waitFor(t, func() bool { return countUnsent(t, st) == 0 }, time.Second)
	cancel()
	<-done
}

func TestRunSizeTriggerFlushesOnBytes(t *testing.T) {
	st := openStore(t)
	mock := newMockCentral()
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	// Byte cap BELOW a single event's payload, huge row cap + age: only the SIZE
	// trigger can fire, and it fires for every batch including a 1-row remainder
	// (so the backlog drains fully rather than starving a sub-threshold tail).
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 10000, MaxBytes: 50, MaxAge: time.Hour, Poll: 5 * time.Millisecond,
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	seedEvents(t, st, 20) // each event's payload (~130B) exceeds the 50B cap
	waitFor(t, func() bool { return countUnsent(t, st) == 0 }, time.Second)
	cancel()
	<-done
}

func TestBackoffResetsOnSuccess(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 1)
	var failuresLeft = 2
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if failuresLeft > 0 {
			failuresLeft--
			http.Error(w, `{"detail":"boom"}`, http.StatusServiceUnavailable)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"applied": 1, "last_seq": 1})
	}))
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 1, MaxAge: 5 * time.Millisecond, Poll: 5 * time.Millisecond,
		Backoff: BackoffConfig{Min: 5 * time.Millisecond, Max: 20 * time.Millisecond},
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	waitFor(t, func() bool { return countUnsent(t, st) == 0 }, time.Second)
	// Stop the loop FIRST (channel-close happens-before) so reading the backoff is
	// race-free, then assert it snapped back to its floor after the success.
	cancel()
	<-done
	if got := rep.backoff.Next(); got != rep.backoff.Min {
		t.Errorf("backoff not reset on success: next=%v, want floor %v", got, rep.backoff.Min)
	}
}

func TestNotFoundIsRetryableWithClearError(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 1)
	mock := newMockCentral()
	mock.notFound = true
	srv := httptest.NewServer(mock.handler())
	defer srv.Close()
	rep := newTestReplicator(t, st, srv, Config{MaxRows: 1})
	_, err := rep.Push(context.Background())
	if err == nil {
		t.Fatal("404 should surface an error")
	}
	if !contains(err.Error(), "disabled") && !contains(err.Error(), "404") {
		t.Errorf("404 error should be clearly worded, got %q", err.Error())
	}
	if n := countUnsent(t, st); n != 1 {
		t.Errorf("404 must not drop the row, %d unsent", n)
	}
}

// recordingObserver captures the drain-health signals for the reconcile wiring.
type recordingObserver struct {
	mu           sync.Mutex
	deadEnds     int
	reconnects   []time.Duration
	deadEndFired chan struct{}
}

func newRecordingObserver() *recordingObserver {
	return &recordingObserver{deadEndFired: make(chan struct{}, 8)}
}

func (o *recordingObserver) CursorDeadEnd() {
	o.mu.Lock()
	o.deadEnds++
	o.mu.Unlock()
	select {
	case o.deadEndFired <- struct{}{}:
	default:
	}
}

func (o *recordingObserver) Reconnected(d time.Duration) {
	o.mu.Lock()
	o.reconnects = append(o.reconnects, d)
	o.mu.Unlock()
}

func (o *recordingObserver) deadEndCount() int {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.deadEnds
}

// TestCursorDeadEndNotifiesObserver proves the run-loop routes an unrecoverable
// gap (fast-forward and rewind both move nothing) to Observer.CursorDeadEnd so the
// reconcile Supervisor can launch a reset sweep (trigger c). It also proves the
// signal is de-duped to once per gap episode (a backing-off drain must not spam).
func TestCursorDeadEndNotifiesObserver(t *testing.T) {
	st := openStore(t)
	seedEvents(t, st, 3) // unsent 1..3
	// Central demands seq 0 forever: below our frontier, nothing to rewind.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]any{"reason": "gap", "expected_seq_no": 0})
	}))
	defer srv.Close()
	obs := newRecordingObserver()
	rep := newTestReplicator(t, st, srv, Config{
		MaxRows: 10, MaxAge: time.Millisecond, Poll: time.Millisecond,
		Backoff:  BackoffConfig{Min: time.Millisecond, Max: 5 * time.Millisecond},
		Observer: obs,
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = rep.Run(ctx) }()

	select {
	case <-obs.deadEndFired:
	case <-time.After(time.Second):
		t.Fatal("dead-end never signalled the observer")
	}
	// Let several more backoff cycles run; the signal must stay de-duped at 1.
	time.Sleep(40 * time.Millisecond)
	cancel()
	<-done
	if n := obs.deadEndCount(); n != 1 {
		t.Errorf("cursor dead-end must fire once per episode, fired %d times", n)
	}
}

func waitFor(t *testing.T, cond func() bool, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", timeout)
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
