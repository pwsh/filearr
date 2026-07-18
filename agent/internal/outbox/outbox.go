// Package outbox is the agent's transactional outbox (research §4.1) and
// replication client (§4.3) for shipping local filesystem changes to central.
//
// # seq_no unification (the durable wire sequence)
//
// P5-T3 stamps a monotonic items.local_seq_no from local_meta on every item
// mutation. P5-T4 does NOT reuse that for the wire. Instead the outbox table's
// AUTOINCREMENT seq_no IS the wire seq_no: it is durable, gap-free-per-agent, and
// AUTOINCREMENT guarantees it is never reused even after a row is marked sent (a
// hard requirement of the central seq-gap guard, backend/filearr/agentsync.py
// check_batch). items.local_seq_no survives untouched as a local bookkeeping
// field (LoadItems ordering, integrity), but it no longer feeds replication.
// Keeping the two separate means a row rewritten twice locally still produces two
// distinct, ordered wire events, and the outbox row — not the item row — is the
// single source of truth for "what central has seen".
//
// # Emission model
//
// A scan calls Write inside the SAME *sql.Tx as each item mutation. Because the
// index helpers (InsertItem/UpdateItem/DeleteItem) are also called during the
// move-detection sentinel dance (parking a survivor at a U+FFFF path, deleting a
// duplicate row), emission is driven from the scan CALL SITES with semantic
// intent — never blindly from the low-level helpers — so a sentinel-parked path
// never leaks onto the wire and a rename collapses to exactly one moved event.
package outbox

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"
)

// Event op values. These MUST match backend/filearr/agentsync.py's EventType
// enum verbatim ("created"|"modified"|"deleted"|"moved") — the design note's
// "upserted" shorthand maps onto created/modified here (central collapses both to
// an upsert via plan_upserts; a fresh row → created, an in-place change or
// self-heal → modified).
const (
	OpCreated  = "created"
	OpModified = "modified"
	OpDeleted  = "deleted"
	OpMoved    = "moved"
)

// Event is the semantic input a scan hands to Write. It is translated into the
// on-wire AgentEvent body (snake_case JSON, backend contract). For OpDeleted the
// file-metadata fields are emitted as JSON null — a tombstone answers "gone",
// central needs only (library_ref, rel_path) to tombstone. FromRelPath is set
// only for OpMoved (the pre-move location).
type Event struct {
	ItemID      string // local item id (bookkeeping/trace only; NOT on the wire)
	Op          string
	LibraryRef  string
	RelPath     string
	FromRelPath string
	Size        int64
	MtimeNs     int64 // local index stores INTEGER unix-nanos; converted below
	QuickHash   string
	ContentHash string
	// ShareHint (P10-T11) is the best-effort network-open location for this file,
	// attached only on created/modified events when local share discovery covers
	// its absolute path. Nil = no hint (the normal case, R1) — omitted from the
	// wire body entirely so an OLD central that predates the field is unaffected.
	ShareHint *ShareHint
}

// ShareHint is the additive share_hint object an agent attaches to a replicated
// event (P10-T11). It rides the EXISTING replication event shape as one optional
// field — no new channel. Absent fields marshal away (omitempty) so the object
// stays minimal and versionable; a central that does not know the field ignores
// it (pydantic default), and an agent with nothing to report omits share_hint
// wholesale. Source is always "agent" (agent-discovered), distinguishing it from
// a central-mapping location.
type ShareHint struct {
	ShareURL  string `json:"share_url"`
	UNC       string `json:"unc,omitempty"`
	ShareName string `json:"share_name,omitempty"`
	Host      string `json:"host,omitempty"`
	Source    string `json:"source"` // always "agent"
}

// eventBody is the AgentEvent JSON minus seq_no (the outbox column supplies it).
// Nil pointers marshal to JSON null so every contract field is present with the
// documented nullable shape. mtime is float epoch SECONDS on the wire. share_hint
// uses omitempty (not null) so its absence is truly absent — the additive-field
// forward/backward-compatibility contract.
type eventBody struct {
	EventType   string     `json:"event_type"`
	LibraryRef  string     `json:"library_ref"`
	RelPath     string     `json:"rel_path"`
	FromRelPath *string    `json:"from_rel_path"`
	Size        *int64     `json:"size"`
	Mtime       *float64   `json:"mtime"`
	QuickHash   *string    `json:"quick_hash"`
	ContentHash *string    `json:"content_hash"`
	ShareHint   *ShareHint `json:"share_hint,omitempty"`
}

// wireEvent is a full AgentEvent (seq_no + body). The embedded eventBody's JSON
// fields are promoted, so marshalling yields the flat contract object and
// unmarshalling a stored payload (which carries no seq_no) leaves SeqNo zero for
// the drain to fill from the column.
type wireEvent struct {
	SeqNo int64 `json:"seq_no"`
	eventBody
}

// nsToWireSeconds converts the local INTEGER unix-nanoseconds mtime to the wire's
// float epoch seconds. Precision note: float64 has a 52-bit mantissa, so at a
// ~1.7e9-second epoch it resolves to well under a microsecond but DROPS
// sub-microsecond nanoseconds. Central stores float seconds by contract, so this
// is the agreed lossy boundary — mtime is a change-detection signal, not an exact
// clock, and a round-trip is stable to microsecond granularity.
func nsToWireSeconds(ns int64) float64 { return float64(ns) / 1e9 }

// body renders the wire body for ev.
func (ev Event) body() eventBody {
	b := eventBody{EventType: ev.Op, LibraryRef: ev.LibraryRef, RelPath: ev.RelPath}
	if ev.FromRelPath != "" {
		fr := ev.FromRelPath
		b.FromRelPath = &fr
	}
	if ev.Op != OpDeleted {
		size := ev.Size
		b.Size = &size
		m := nsToWireSeconds(ev.MtimeNs)
		b.Mtime = &m
		if ev.QuickHash != "" {
			q := ev.QuickHash
			b.QuickHash = &q
		}
		if ev.ContentHash != "" {
			c := ev.ContentHash
			b.ContentHash = &c
		}
		// A hint is only meaningful for a present file (created/modified). It is
		// omitted for a delete (nothing to open) and whenever discovery yields
		// nothing (ev.ShareHint == nil), keeping share_hint truly absent.
		if ev.ShareHint != nil {
			b.ShareHint = ev.ShareHint
		}
	}
	return b
}

// Write appends ev to the outbox inside tx and returns the allocated wire seq_no.
// It MUST share the caller's item-mutation transaction: if tx rolls back, neither
// the item change nor its event lands (the atomicity the whole design rests on).
func Write(ctx context.Context, tx *sql.Tx, ev Event) (int64, error) {
	if ev.Op == "" || ev.LibraryRef == "" || ev.RelPath == "" {
		return 0, fmt.Errorf("outbox: incomplete event (op=%q library_ref=%q rel_path=%q)", ev.Op, ev.LibraryRef, ev.RelPath)
	}
	payload, err := json.Marshal(ev.body())
	if err != nil {
		return 0, fmt.Errorf("outbox: marshal payload: %w", err)
	}
	var seq int64
	err = tx.QueryRowContext(ctx,
		`INSERT INTO outbox(item_id, op, payload, written_at)
		 VALUES(?, ?, ?, ?) RETURNING seq_no`,
		ev.ItemID, ev.Op, string(payload), nowUTC(),
	).Scan(&seq)
	if err != nil {
		return 0, fmt.Errorf("outbox: insert row: %w", err)
	}
	return seq, nil
}

// Row is one unsent outbox record read by the drain.
type Row struct {
	SeqNo     int64
	WrittenAt time.Time
	Payload   string // AgentEvent JSON minus seq_no
}

// Outbox owns drain-side reads and cursor bookkeeping against the shared SQLite
// database. Emission uses the free Write function (it needs the scan's *sql.Tx);
// the drain reads/marks run on their own connection between batches.
type Outbox struct {
	db *sql.DB
}

// New wraps the store's *sql.DB (index.Store.DB()).
func New(db *sql.DB) *Outbox { return &Outbox{db: db} }

// Unsent returns up to limit unsent rows in ascending seq_no order — the wire
// order central's contiguity guard requires.
func (o *Outbox) Unsent(ctx context.Context, limit int) ([]Row, error) {
	rows, err := o.db.QueryContext(ctx,
		`SELECT seq_no, written_at, payload FROM outbox
		 WHERE sent_at IS NULL ORDER BY seq_no LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("outbox: query unsent: %w", err)
	}
	defer rows.Close()
	var out []Row
	for rows.Next() {
		var (
			r         Row
			writtenAt string
		)
		if err := rows.Scan(&r.SeqNo, &writtenAt, &r.Payload); err != nil {
			return nil, err
		}
		r.WrittenAt = parseTS(writtenAt)
		out = append(out, r)
	}
	return out, rows.Err()
}

// CountUnsent returns how many rows remain undrained (for `push` reporting).
func (o *Outbox) CountUnsent(ctx context.Context) (int, error) {
	var n int
	err := o.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL`).Scan(&n)
	if err != nil {
		return 0, fmt.Errorf("outbox: count unsent: %w", err)
	}
	return n, nil
}

// MarkSent stamps sent_at+batch_id on the contiguous [fromSeq, toSeq] span that a
// central ACK acknowledged. Only still-unsent rows are touched (idempotent under
// a duplicate ACK). ctx should be detached from shutdown cancellation so a clean
// stop right after a 200 still records the mark (see Replicator).
func (o *Outbox) MarkSent(ctx context.Context, fromSeq, toSeq int64, batchID string) (int64, error) {
	res, err := o.db.ExecContext(ctx,
		`UPDATE outbox SET sent_at = ?, batch_id = ?
		 WHERE seq_no BETWEEN ? AND ? AND sent_at IS NULL`,
		nowUTC(), batchID, fromSeq, toSeq)
	if err != nil {
		return 0, fmt.Errorf("outbox: mark sent: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// SetCursor reconciles the drain frontier to central's expected_seq_no from a 409
// (backend agentsync.check_batch's resend_from):
//
//   - fast-forward: any still-unsent row below expected is one central already
//     applied (a stale/duplicate replay) — mark it sent so the drain skips it.
//   - rewind: any row at/after expected that we had marked sent is un-marked so it
//     re-drains from expected (central lost/never-committed it).
//
// Both run in one transaction. Returns how many rows each arm touched; when BOTH
// are zero the cursor did not move (central wants seq the agent cannot supply —
// an unrecoverable gap the caller escalates rather than hot-looping).
func (o *Outbox) SetCursor(ctx context.Context, expected int64, batchID string) (forwarded, rewound int64, err error) {
	tx, err := o.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, 0, fmt.Errorf("outbox: begin cursor tx: %w", err)
	}
	defer func() {
		if tx != nil {
			_ = tx.Rollback()
		}
	}()
	res, err := tx.ExecContext(ctx,
		`UPDATE outbox SET sent_at = ?, batch_id = ?
		 WHERE seq_no < ? AND sent_at IS NULL`, nowUTC(), batchID, expected)
	if err != nil {
		return 0, 0, fmt.Errorf("outbox: fast-forward: %w", err)
	}
	forwarded, _ = res.RowsAffected()
	res, err = tx.ExecContext(ctx,
		`UPDATE outbox SET sent_at = NULL, batch_id = NULL
		 WHERE seq_no >= ? AND sent_at IS NOT NULL`, expected)
	if err != nil {
		return 0, 0, fmt.Errorf("outbox: rewind: %w", err)
	}
	rewound, _ = res.RowsAffected()
	c := tx
	tx = nil
	if err := c.Commit(); err != nil {
		return 0, 0, fmt.Errorf("outbox: commit cursor: %w", err)
	}
	return forwarded, rewound, nil
}

// MarkAllSent stamps sent_at+batch_id on EVERY still-unsent row. It is the
// P5-T5 reset-reconcile epilogue: after central resets the agent's replication
// watermark from a full-manifest sweep (finish reset_seq=true), the outbox
// backlog is superseded by the reconciled state, so those rows must never
// replay. Returns how many rows were marked. Idempotent (a second call marks
// zero). ctx should be detached from shutdown cancellation for the same reason
// MarkSent is (a clean stop right after the reconciled ACK must still persist).
func (o *Outbox) MarkAllSent(ctx context.Context, batchID string) (int64, error) {
	res, err := o.db.ExecContext(ctx,
		`UPDATE outbox SET sent_at = ?, batch_id = ? WHERE sent_at IS NULL`,
		nowUTC(), batchID)
	if err != nil {
		return 0, fmt.Errorf("outbox: mark all sent: %w", err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// IsEmpty reports whether the outbox has ever held a row (any row, sent or not).
// AUTOINCREMENT never reuses seq_no and nothing deletes outbox rows, so an empty
// table means the outbox was (re)created and never written — the P5-T5 fallback
// "index was rebuilt" signal when combined with a non-empty item set (Store.
// Rebuilt is process-lifetime only and lost across a restart).
func (o *Outbox) IsEmpty(ctx context.Context) (bool, error) {
	var n int
	if err := o.db.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM outbox)`).Scan(&n); err != nil {
		return false, fmt.Errorf("outbox: probe empty: %w", err)
	}
	return n == 0, nil
}

func nowUTC() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func parseTS(s string) time.Time {
	if s == "" {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}
	}
	return t
}
