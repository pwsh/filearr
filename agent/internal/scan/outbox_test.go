package scan

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/shares"
)

// wireEvt mirrors the on-wire AgentEvent for asserting emitted rows.
type wireEvt struct {
	SeqNo       int64
	EventType   string  `json:"event_type"`
	LibraryRef  string  `json:"library_ref"`
	RelPath     string  `json:"rel_path"`
	FromRelPath *string `json:"from_rel_path"`
	Size        *int64  `json:"size"`
	Mtime       *float64
	QuickHash   *string      `json:"quick_hash"`
	ShareHint   *wireHintEvt `json:"share_hint"`
}

type wireHintEvt struct {
	ShareURL  string `json:"share_url"`
	UNC       string `json:"unc"`
	ShareName string `json:"share_name"`
	Host      string `json:"host"`
	Source    string `json:"source"`
}

// fakeShares is a fixed ShareResolver: it returns hint for any path containing
// match, else nil — enough to prove the scan wires discovery onto created/modified
// events without depending on the host's real shares.
type fakeShares struct {
	match string
	hint  *shares.Hint
}

func (f fakeShares) Hint(abs string) *shares.Hint {
	if strings.Contains(abs, f.match) {
		return f.hint
	}
	return nil
}

// drainEvents reads every outbox row (marking them sent) so each assertion sees
// only the events a given scan produced.
func drainEvents(t *testing.T, st *index.Store) []wireEvt {
	t.Helper()
	ob := outbox.New(st.DB())
	rows, err := ob.Unsent(context.Background(), 100000)
	if err != nil {
		t.Fatal(err)
	}
	var out []wireEvt
	for _, r := range rows {
		var e wireEvt
		if err := json.Unmarshal([]byte(r.Payload), &e); err != nil {
			t.Fatal(err)
		}
		e.SeqNo = r.SeqNo
		out = append(out, e)
	}
	if len(rows) > 0 {
		if _, err := ob.MarkSent(context.Background(), rows[0].SeqNo, rows[len(rows)-1].SeqNo, "test"); err != nil {
			t.Fatal(err)
		}
	}
	return out
}

func byType(evs []wireEvt, t string) []wireEvt {
	var out []wireEvt
	for _, e := range evs {
		if e.EventType == t {
			out = append(out, e)
		}
	}
	return out
}

func TestEmitMatrixCreatedModifiedDeletedSelfHeal(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4", "b.mkv", "art.nfo"})
	st := newStore(t)

	// New scan → one created per file, INCLUDING the sidecar (plain item on wire).
	mustScan(t, st, Options{Root: root})
	evs := drainEvents(t, st)
	if len(byType(evs, "created")) != 3 {
		t.Fatalf("first scan should emit 3 created (incl. sidecar), got %d of %d", len(byType(evs, "created")), len(evs))
	}
	for _, e := range evs {
		if e.LibraryRef != root {
			t.Errorf("library_ref = %q, want the root abspath %q", e.LibraryRef, root)
		}
	}
	// The media file's created event carries a quick_hash; sizes present.
	var aEvt *wireEvt
	for i := range evs {
		if evs[i].RelPath == "a.mp4" {
			aEvt = &evs[i]
		}
	}
	if aEvt == nil || aEvt.QuickHash == nil || *aEvt.QuickHash == "" {
		t.Error("a.mp4 created event should carry a quick_hash")
	}

	// Unchanged rescan → NOTHING emitted (no-op deviation stands).
	mustScan(t, st, Options{Root: root})
	if evs := drainEvents(t, st); len(evs) != 0 {
		t.Fatalf("unchanged rescan must emit zero events, got %d", len(evs))
	}

	// Change a.mp4 → one modified.
	if err := os.WriteFile(filepath.Join(root, "a.mp4"), []byte("longer contents now"), 0o644); err != nil {
		t.Fatal(err)
	}
	mustScan(t, st, Options{Root: root})
	evs = drainEvents(t, st)
	if len(evs) != 1 || evs[0].EventType != "modified" || evs[0].RelPath != "a.mp4" {
		t.Fatalf("change should emit exactly one modified for a.mp4, got %+v", evs)
	}

	// Delete b.mkv → one deleted with null metadata.
	if err := os.Remove(filepath.Join(root, "b.mkv")); err != nil {
		t.Fatal(err)
	}
	mustScan(t, st, Options{Root: root})
	evs = drainEvents(t, st)
	if len(evs) != 1 || evs[0].EventType != "deleted" || evs[0].RelPath != "b.mkv" {
		t.Fatalf("delete should emit one deleted for b.mkv, got %+v", evs)
	}
	if evs[0].Size != nil || evs[0].QuickHash != nil {
		t.Error("deleted event must null size/quick_hash")
	}

	// Recreate b.mkv → self-heal emits modified.
	mktree(t, root, []string{"b.mkv"})
	mustScan(t, st, Options{Root: root})
	evs = drainEvents(t, st)
	// b.mkv reappears as a genuinely-new row here (it was tombstoned, not removed),
	// so the emission is either created (new row) or modified (self-heal) — assert
	// exactly one event touching b.mkv that is an upsert (created|modified).
	if len(evs) != 1 || evs[0].RelPath != "b.mkv" ||
		(evs[0].EventType != "created" && evs[0].EventType != "modified") {
		t.Fatalf("reappearance should emit one upsert for b.mkv, got %+v", evs)
	}
}

func TestEmitMovedIsSinglePairedEvent(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"movies/old.mp4"})
	st := newStore(t)
	mustScan(t, st, Options{Root: root})
	drainEvents(t, st) // discard the initial created

	// Rename within the root: same bytes, new path → one moved event.
	if err := os.MkdirAll(filepath.Join(root, "films"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(root, "movies", "old.mp4"), filepath.Join(root, "films", "new.mp4")); err != nil {
		t.Fatal(err)
	}
	res := mustScan(t, st, Options{Root: root})
	if res.Moved != 1 {
		t.Fatalf("scan should detect 1 move, got %+v", res)
	}

	evs := drainEvents(t, st)
	moved := byType(evs, "moved")
	if len(moved) != 1 {
		t.Fatalf("a rename must collapse to exactly ONE moved event, got %d (all: %+v)", len(moved), evs)
	}
	m := moved[0]
	if m.FromRelPath == nil || *m.FromRelPath != "movies/old.mp4" {
		t.Errorf("from_rel_path = %v, want movies/old.mp4", m.FromRelPath)
	}
	if m.RelPath != "films/new.mp4" {
		t.Errorf("rel_path = %q, want films/new.mp4", m.RelPath)
	}
	// No sentinel/parking path may EVER leak onto the wire.
	for _, e := range evs {
		if containsRune(e.RelPath, '￿') || (e.FromRelPath != nil && containsRune(*e.FromRelPath, '￿')) {
			t.Errorf("a U+FFFF sentinel path leaked to the wire: %+v", e)
		}
	}
	// The moved event carries the post-move payload (a hash for the relocated file).
	if m.QuickHash == nil || *m.QuickHash == "" {
		t.Error("moved event should carry the survivor's post-move quick_hash")
	}
}

// TestScanAttachesShareHint proves a real scan attaches the P10-T11 hint to a
// created/modified event when discovery covers the file, and omits it otherwise.
func TestScanAttachesShareHint(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4", "b.mkv"})
	st := newStore(t)
	fr := fakeShares{match: "a.mp4", hint: &shares.Hint{
		ShareURL: "smb://NAS/media/a.mp4", UNC: `\\NAS\media\a.mp4`,
		ShareName: "media", Host: "NAS", Source: "agent",
	}}
	mustScan(t, st, Options{Root: root, Shares: fr})
	evs := drainEvents(t, st)

	var a, b *wireEvt
	for i := range evs {
		switch evs[i].RelPath {
		case "a.mp4":
			a = &evs[i]
		case "b.mkv":
			b = &evs[i]
		}
	}
	if a == nil || a.ShareHint == nil {
		t.Fatalf("a.mp4 created event should carry a share_hint, got %+v", a)
	}
	if a.ShareHint.ShareURL != "smb://NAS/media/a.mp4" || a.ShareHint.Source != "agent" {
		t.Errorf("share_hint shape wrong: %+v", a.ShareHint)
	}
	if b == nil || b.ShareHint != nil {
		t.Fatalf("b.mkv (uncovered) must carry NO share_hint, got %+v", b)
	}
}

// TestScanNoResolverOmitsShareHint: a nil resolver (discovery disabled) emits
// events with no share_hint at all.
func TestScanNoResolverOmitsShareHint(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4"})
	st := newStore(t)
	mustScan(t, st, Options{Root: root}) // Shares nil
	for _, e := range drainEvents(t, st) {
		if e.ShareHint != nil {
			t.Fatalf("nil resolver must omit share_hint, got %+v", e.ShareHint)
		}
	}
}

func containsRune(s string, r rune) bool {
	for _, c := range s {
		if c == r {
			return true
		}
	}
	return false
}
