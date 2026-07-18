package outbox

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// wireForTest mirrors the on-wire AgentEvent so tests assert exact field shape
// (snake_case, nullable pointers) against backend/filearr/agentsync.py.
type wireForTest struct {
	SeqNo       int64    `json:"seq_no"`
	EventType   string   `json:"event_type"`
	LibraryRef  string   `json:"library_ref"`
	RelPath     string   `json:"rel_path"`
	FromRelPath *string  `json:"from_rel_path"`
	Size        *int64   `json:"size"`
	Mtime       *float64 `json:"mtime"`
	QuickHash   *string  `json:"quick_hash"`
	ContentHash *string  `json:"content_hash"`
}

func openStore(t *testing.T) *index.Store {
	t.Helper()
	st, err := index.Open(filepath.Join(t.TempDir(), "index.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

// writeOne emits a single event in its own committed tx and returns its seq_no.
func writeOne(t *testing.T, st *index.Store, ev Event) int64 {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	seq, err := Write(ctx, tx, ev)
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return seq
}

// firstUnsent parses the single expected unsent payload into wireForTest.
func firstUnsent(t *testing.T, st *index.Store) wireForTest {
	t.Helper()
	rows, err := New(st.DB()).Unsent(context.Background(), 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("want exactly 1 unsent row, got %d", len(rows))
	}
	var w wireForTest
	if err := json.Unmarshal([]byte(rows[0].Payload), &w); err != nil {
		t.Fatal(err)
	}
	w.SeqNo = rows[0].SeqNo // payload carries no seq_no; column is authoritative
	return w
}

func TestEventFieldsUpserted(t *testing.T) {
	st := openStore(t)
	const ns = int64(1_700_000_000_123_456_789)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/media/movies", RelPath: "A/B.mkv",
		Size: 42, MtimeNs: ns, QuickHash: "qh", ContentHash: "ch",
	})
	w := firstUnsent(t, st)

	if w.EventType != "created" {
		t.Errorf("event_type = %q, want created", w.EventType)
	}
	if w.LibraryRef != "/media/movies" || w.RelPath != "A/B.mkv" {
		t.Errorf("library_ref/rel_path wrong: %+v", w)
	}
	if w.FromRelPath != nil {
		t.Errorf("from_rel_path must be null for a non-move, got %v", *w.FromRelPath)
	}
	if w.Size == nil || *w.Size != 42 {
		t.Errorf("size = %v, want 42", w.Size)
	}
	if w.QuickHash == nil || *w.QuickHash != "qh" || w.ContentHash == nil || *w.ContentHash != "ch" {
		t.Errorf("hashes wrong: %+v", w)
	}
	// mtime is float epoch SECONDS, resolved to ~microsecond precision.
	if w.Mtime == nil {
		t.Fatal("mtime must be present for an upsert")
	}
	if got, want := *w.Mtime, float64(ns)/1e9; got != want {
		t.Errorf("mtime = %.9f, want %.9f (ns→float seconds)", got, want)
	}
	if *w.Mtime < 1.7e9 || *w.Mtime > 1.8e9 {
		t.Errorf("mtime %.6f is not plausibly epoch SECONDS", *w.Mtime)
	}
	if w.SeqNo != 1 {
		t.Errorf("first outbox seq_no = %d, want 1 (AUTOINCREMENT starts at 1)", w.SeqNo)
	}
}

func TestEventFieldsDeletedNullsMetadata(t *testing.T) {
	st := openStore(t)
	// Even though the tombstoned item still has size/mtime/hashes locally, a
	// deleted event carries them as null — central tombstones on (library, path).
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpDeleted, LibraryRef: "/m", RelPath: "gone.mkv",
		Size: 99, MtimeNs: 123, QuickHash: "q", ContentHash: "c",
	})
	w := firstUnsent(t, st)
	if w.EventType != "deleted" {
		t.Errorf("event_type = %q, want deleted", w.EventType)
	}
	if w.Size != nil || w.Mtime != nil || w.QuickHash != nil || w.ContentHash != nil {
		t.Errorf("deleted event must null size/mtime/hashes, got %+v", w)
	}
}

func TestEventFieldsMovedCarriesFromRelPath(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpMoved, LibraryRef: "/m", RelPath: "new/here.mkv",
		FromRelPath: "old/there.mkv", Size: 10, MtimeNs: 2_000_000_000, QuickHash: "q",
	})
	w := firstUnsent(t, st)
	if w.EventType != "moved" {
		t.Errorf("event_type = %q, want moved", w.EventType)
	}
	if w.FromRelPath == nil || *w.FromRelPath != "old/there.mkv" {
		t.Errorf("from_rel_path = %v, want old/there.mkv", w.FromRelPath)
	}
	if w.RelPath != "new/here.mkv" {
		t.Errorf("rel_path = %q, want new/here.mkv", w.RelPath)
	}
	if w.Size == nil || *w.Size != 10 {
		t.Errorf("moved event carries the post-move payload, size=%v", w.Size)
	}
}

// rawPayload returns the single unsent row's raw JSON payload for key-presence
// assertions (the additive share_hint field's omitempty behaviour).
func rawPayload(t *testing.T, st *index.Store) map[string]any {
	t.Helper()
	rows, err := New(st.DB()).Unsent(context.Background(), 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("want exactly 1 unsent row, got %d", len(rows))
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(rows[0].Payload), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// TestShareHintAttachedOnUpsert verifies the additive P10-T11 share_hint object
// rides a created/modified event with its exact snake_case shape.
func TestShareHintAttachedOnUpsert(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/m", RelPath: "a.mkv",
		Size: 1, MtimeNs: 1, QuickHash: "q",
		ShareHint: &ShareHint{
			ShareURL: "smb://NAS/media/a.mkv", UNC: `\\NAS\media\a.mkv`,
			ShareName: "media", Host: "NAS", Source: "agent",
		},
	})
	m := rawPayload(t, st)
	sh, ok := m["share_hint"].(map[string]any)
	if !ok {
		t.Fatalf("share_hint missing/not an object: %v", m["share_hint"])
	}
	if sh["share_url"] != "smb://NAS/media/a.mkv" || sh["unc"] != `\\NAS\media\a.mkv` ||
		sh["share_name"] != "media" || sh["host"] != "NAS" || sh["source"] != "agent" {
		t.Fatalf("share_hint shape wrong: %+v", sh)
	}
}

// TestShareHintOmittedWhenAbsent pins the forward/backward-compatibility contract:
// with no hint the share_hint KEY is entirely absent (omitempty), so an old central
// that predates the field is unaffected.
func TestShareHintOmittedWhenAbsent(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/m", RelPath: "a.mkv",
		Size: 1, MtimeNs: 1, QuickHash: "q", // ShareHint left nil
	})
	m := rawPayload(t, st)
	if _, present := m["share_hint"]; present {
		t.Fatalf("share_hint must be OMITTED when absent, got %v", m["share_hint"])
	}
}

// TestShareHintNotEmittedOnDelete confirms a tombstone never carries a hint even
// if one is (defensively) supplied — there is nothing to open.
func TestShareHintNotEmittedOnDelete(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpDeleted, LibraryRef: "/m", RelPath: "gone.mkv",
		ShareHint: &ShareHint{ShareURL: "smb://NAS/media/gone.mkv", Source: "agent"},
	})
	m := rawPayload(t, st)
	if _, present := m["share_hint"]; present {
		t.Fatalf("a deleted event must not carry share_hint, got %v", m["share_hint"])
	}
}

// TestOutboxWriteIsAtomicWithItemMutation is the core invariant: the event and
// its item mutation share one *sql.Tx, so a rolled-back batch leaves NEITHER.
func TestOutboxWriteIsAtomicWithItemMutation(t *testing.T) {
	st := openStore(t)
	ctx := context.Background()

	// Root in a committed tx (FK target).
	rtx, _ := st.Begin(ctx)
	rootID, err := index.EnsureRoot(ctx, rtx, "/media")
	if err != nil {
		t.Fatal(err)
	}
	if err := rtx.Commit(); err != nil {
		t.Fatal(err)
	}

	// Item + event in ONE tx, then FORCE a rollback (fault injection).
	tx, _ := st.Begin(ctx)
	id, _ := index.NewID()
	it := &index.Item{
		ID: id, RootID: rootID, RelPath: "x.mkv", Filename: "x.mkv",
		Size: 1, MtimeNs: 1, Status: index.StatusActive,
		FirstSeen: time.Now(), LastSeen: time.Now(),
	}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if _, err := Write(ctx, tx, Event{ItemID: id, Op: OpCreated, LibraryRef: "/media", RelPath: "x.mkv", Size: 1, MtimeNs: 1}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	if n := countRows(t, st.DB(), "SELECT COUNT(*) FROM items"); n != 0 {
		t.Errorf("rolled-back item persisted: %d rows", n)
	}
	if n := countRows(t, st.DB(), "SELECT COUNT(*) FROM outbox"); n != 0 {
		t.Errorf("rolled-back event persisted: %d rows — outbox is NOT atomic with the item", n)
	}
}

func TestSeqNoMonotonicAndDurableAcrossMarks(t *testing.T) {
	st := openStore(t)
	s1 := writeOne(t, st, Event{ItemID: "a", Op: OpCreated, LibraryRef: "/m", RelPath: "a", Size: 1, MtimeNs: 1})
	// Mark the first row sent, then write another: AUTOINCREMENT must NOT reuse s1.
	if _, err := New(st.DB()).MarkSent(context.Background(), s1, s1, "b1"); err != nil {
		t.Fatal(err)
	}
	s2 := writeOne(t, st, Event{ItemID: "b", Op: OpCreated, LibraryRef: "/m", RelPath: "b", Size: 1, MtimeNs: 1})
	if !(s2 > s1) {
		t.Errorf("seq_no must be strictly increasing and never reused: s1=%d s2=%d", s1, s2)
	}
}

func TestSetCursorFastForward(t *testing.T) {
	st := openStore(t)
	ob := New(st.DB())
	for i := 0; i < 5; i++ {
		writeOne(t, st, Event{ItemID: "i", Op: OpCreated, LibraryRef: "/m", RelPath: "p", Size: 1, MtimeNs: 1})
	}
	// Central already has 1..3 → expected 4. Fast-forward marks 1..3 sent.
	fwd, rwd, err := ob.SetCursor(context.Background(), 4, "bx")
	if err != nil {
		t.Fatal(err)
	}
	if fwd != 3 || rwd != 0 {
		t.Fatalf("fast-forward to 4: forwarded=%d rewound=%d, want 3/0", fwd, rwd)
	}
	rows, _ := ob.Unsent(context.Background(), 100)
	if len(rows) != 2 || rows[0].SeqNo != 4 {
		t.Errorf("unsent frontier should start at 4, got %+v", seqs(rows))
	}
}

func TestSetCursorRewind(t *testing.T) {
	st := openStore(t)
	ob := New(st.DB())
	for i := 0; i < 5; i++ {
		writeOne(t, st, Event{ItemID: "i", Op: OpCreated, LibraryRef: "/m", RelPath: "p", Size: 1, MtimeNs: 1})
	}
	// Mark 1..5 all sent, then central says it only has up to 2 → expected 3.
	if _, err := ob.MarkSent(context.Background(), 1, 5, "b1"); err != nil {
		t.Fatal(err)
	}
	fwd, rwd, err := ob.SetCursor(context.Background(), 3, "bx")
	if err != nil {
		t.Fatal(err)
	}
	if fwd != 0 || rwd != 3 {
		t.Fatalf("rewind to 3: forwarded=%d rewound=%d, want 0/3 (seqs 3,4,5)", fwd, rwd)
	}
	rows, _ := ob.Unsent(context.Background(), 100)
	if len(rows) != 3 || rows[0].SeqNo != 3 {
		t.Errorf("rewind should re-expose 3,4,5, got %+v", seqs(rows))
	}
}

func TestSetCursorNoMovementIsDetectable(t *testing.T) {
	st := openStore(t)
	ob := New(st.DB())
	// Unsent rows 1..3; central expects 1 (which we already hold as the frontier)
	// with nothing below and nothing sent to rewind → no movement.
	for i := 0; i < 3; i++ {
		writeOne(t, st, Event{ItemID: "i", Op: OpCreated, LibraryRef: "/m", RelPath: "p", Size: 1, MtimeNs: 1})
	}
	fwd, rwd, err := ob.SetCursor(context.Background(), 1, "bx")
	if err != nil {
		t.Fatal(err)
	}
	if fwd != 0 || rwd != 0 {
		t.Fatalf("no-op cursor: forwarded=%d rewound=%d, want 0/0 (the unrecoverable-gap signal)", fwd, rwd)
	}
}

func TestIsEmpty(t *testing.T) {
	st := openStore(t)
	ob := New(st.DB())
	empty, err := ob.IsEmpty(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !empty {
		t.Fatal("a fresh outbox must report empty")
	}
	writeOne(t, st, Event{ItemID: "i", Op: OpCreated, LibraryRef: "/m", RelPath: "p", Size: 1, MtimeNs: 1})
	// Even after marking that row sent, the table is non-empty (rows persist).
	if _, err := ob.MarkAllSent(context.Background(), "b"); err != nil {
		t.Fatal(err)
	}
	empty, err = ob.IsEmpty(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if empty {
		t.Error("a written-then-sent outbox is NOT empty (sent rows persist)")
	}
}

func TestMarkAllSentSupersedesBacklog(t *testing.T) {
	st := openStore(t)
	ob := New(st.DB())
	for i := 0; i < 5; i++ {
		writeOne(t, st, Event{ItemID: "i", Op: OpCreated, LibraryRef: "/m", RelPath: "p", Size: 1, MtimeNs: 1})
	}
	// Pre-mark one row sent so we prove MarkAllSent only touches the still-unsent
	// rows and is additive/idempotent.
	if _, err := ob.MarkSent(context.Background(), 1, 1, "old"); err != nil {
		t.Fatal(err)
	}
	n, err := ob.MarkAllSent(context.Background(), "reset")
	if err != nil {
		t.Fatal(err)
	}
	if n != 4 {
		t.Errorf("MarkAllSent marked %d, want the 4 remaining unsent rows", n)
	}
	if c, _ := ob.CountUnsent(context.Background()); c != 0 {
		t.Errorf("no rows may remain unsent after MarkAllSent, got %d", c)
	}
	// Idempotent: a second call marks zero.
	again, err := ob.MarkAllSent(context.Background(), "reset2")
	if err != nil {
		t.Fatal(err)
	}
	if again != 0 {
		t.Errorf("second MarkAllSent must be a no-op, marked %d", again)
	}
}

func countRows(t *testing.T, db *sql.DB, q string) int {
	t.Helper()
	var n int
	if err := db.QueryRow(q).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func seqs(rows []Row) []int64 {
	out := make([]int64, len(rows))
	for i, r := range rows {
		out[i] = r.SeqNo
	}
	return out
}
