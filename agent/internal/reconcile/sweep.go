package reconcile

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"

	"github.com/google/uuid"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// Sweeper runs the full-manifest reconciliation for every local root: build the
// manifest → digest → start; on a mismatch, page the rows and finish. It owns the
// restart-once recovery (session expiry / digest mismatch) and, on a reset sweep,
// the outbox-supersede epilogue.
type Sweeper struct {
	store  *index.Store
	ob     *outbox.Outbox
	client *Client
	log    *slog.Logger
}

// NewSweeper wires a Sweeper over the local store/outbox and a reconcile Client.
func NewSweeper(store *index.Store, ob *outbox.Outbox, client *Client, log *slog.Logger) *Sweeper {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &Sweeper{store: store, ob: ob, client: client, log: log}
}

// RootResult is the per-root outcome of a sweep.
type RootResult struct {
	LibraryRef string
	RowCount   int
	Matched    bool         // digest matched central on start; no rows streamed
	Reset      bool         // reset_seq was sent on finish
	Finish     FinishResult // central's counters (only when reconciled)
	Err        error        // per-root terminal error (after restart-once)
}

// SweepResult aggregates a full sweep across all roots.
type SweepResult struct {
	Roots        []RootResult
	Rebuilt      bool  // the rebuilt signal sent in start (index was rebuilt)
	Reset        bool  // reset_seq requested (rebuilt OR a forced dead-end reset)
	OutboxMarked int64 // outbox rows superseded via MarkAllSent on a reset sweep
}

// Options tune a single sweep.
type Options struct {
	// ForceReset forces reset_seq=true (and the outbox-supersede epilogue) even
	// without a rebuild — the replication cursor-dead-end repair path (trigger c).
	ForceReset bool
}

// Sweep reconciles every root once and returns the aggregate result. A per-root
// failure is recorded in RootResult.Err and does not abort the remaining roots;
// the returned error is non-nil iff at least one root failed terminally (so a
// caller can surface it) — but the outbox-supersede epilogue still runs for the
// roots that did reconcile, because a reset sweep's whole point is to move past
// an unsendable backlog.
func (s *Sweeper) Sweep(ctx context.Context, opts Options) (SweepResult, error) {
	rebuilt, err := s.rebuiltSignal(ctx)
	if err != nil {
		return SweepResult{}, err
	}
	reset := rebuilt || opts.ForceReset
	res := SweepResult{Rebuilt: rebuilt, Reset: reset}

	roots, err := s.store.Roots(ctx)
	if err != nil {
		return res, fmt.Errorf("reconcile: list roots: %w", err)
	}

	var firstErr error
	for _, root := range roots {
		rr := s.sweepRoot(ctx, root, rebuilt, reset)
		res.Roots = append(res.Roots, rr)
		if rr.Err != nil && firstErr == nil {
			firstErr = fmt.Errorf("reconcile root %s: %w", root.Path, rr.Err)
		}
	}

	// Reset epilogue: supersede the outbox backlog so a rebuilt/reset seq base does
	// not replay stale rows (and so a dead-end drain stops hot-looping). Runs once
	// per sweep, independent of per-root match/mismatch.
	if reset {
		marked, mErr := s.ob.MarkAllSent(context.WithoutCancel(ctx), "reconcile-"+newBatchID())
		if mErr != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("reconcile: supersede outbox: %w", mErr)
			}
		} else {
			res.OutboxMarked = marked
			s.log.Info("reconcile superseded outbox backlog", "marked", marked)
		}
	}

	// Clear the durable rebuilt marker ONLY after a successful sweep that actually
	// carried rebuilt=true to central (≥1 root reconciled/matched, no terminal
	// error). A failed sweep — or a marker-set agent with no roots yet to carry the
	// signal — leaves it set so the NEXT sweep retries. A clear failure is not
	// fatal: the marker persisting only costs a harmless no-op rebuilt=true later.
	if rebuilt && firstErr == nil && len(res.Roots) > 0 {
		if err := s.store.ClearRebuiltPending(context.WithoutCancel(ctx)); err != nil {
			s.log.Warn("reconcile: could not clear rebuilt marker (persists for next sweep)", "err", err)
		} else {
			s.log.Info("reconcile cleared durable rebuilt marker")
		}
	}
	return res, firstErr
}

// sweepRoot reconciles one root with restart-once semantics: a session expiry
// (404) or a finish digest mismatch (409) restarts the whole root once; a second
// occurrence is surfaced.
func (s *Sweeper) sweepRoot(ctx context.Context, root index.RootRef, rebuilt, reset bool) RootResult {
	items, err := s.store.ActiveItems(ctx, root.ID)
	if err != nil {
		return RootResult{LibraryRef: root.Path, Err: fmt.Errorf("project manifest: %w", err)}
	}
	rows := ProjectItems(items)
	digest := Digest(rows)
	rr := RootResult{LibraryRef: root.Path, RowCount: len(rows), Reset: reset}

	const maxAttempts = 2 // initial + one restart
	for attempt := 0; attempt < maxAttempts; attempt++ {
		matched, sid, err := s.client.Start(ctx, root.Path, digest, len(rows), rebuilt)
		if err != nil {
			rr.Err = err
			return rr
		}
		if matched {
			rr.Matched = true
			rr.Err = nil
			return rr
		}
		if err := s.client.SendRows(ctx, sid, rows); err != nil {
			if errors.Is(err, ErrSessionExpired) && attempt == 0 {
				s.log.Warn("reconcile session expired during rows; restarting sweep", "root", root.Path)
				continue
			}
			rr.Err = err
			return rr
		}
		fin, err := s.client.Finish(ctx, sid, digest, len(rows), reset)
		if err != nil {
			if (errors.Is(err, ErrSessionExpired) || errors.Is(err, ErrDigestMismatch)) && attempt == 0 {
				s.log.Warn("reconcile finish needs restart", "root", root.Path, "err", err)
				continue
			}
			rr.Err = err
			return rr
		}
		rr.Finish = fin
		rr.Err = nil
		return rr
	}
	// Exhausted the restart: the last continue fell through without success.
	if rr.Err == nil {
		rr.Err = fmt.Errorf("reconcile root %s: restart exhausted", root.Path)
	}
	return rr
}

// rebuiltSignal decides whether this agent's index counts as freshly rebuilt,
// meaning central must reset its per-agent seq watermark (else the agent's fresh
// low seq_no rows are silently fast-forwarded away as stale — the E2E gap this
// fixes).
//
// Three signals, in priority order:
//
//  1. RebuiltPending — the DURABLE marker written by index.Open when it
//     fresh-created OR corruption-rebuilt the database. This is the primary,
//     process-boundary-safe signal: `scan` rebuilds (marker written), a SEPARATE
//     `reconcile` process still sees it. Cleared only after a successful
//     rebuilt-carrying sweep.
//  2. Store.Rebuilt — the in-memory flag for the SAME process that did the
//     rebuild (redundant with the marker, kept as a belt-and-braces signal).
//  3. Empty outbox WITH active items — a legacy fallback for a store whose outbox
//     was emptied out-of-band while items remain. Rarely the sole signal now that
//     (1) covers fresh-create and rebuild, but harmless and cheap.
func (s *Sweeper) rebuiltSignal(ctx context.Context) (bool, error) {
	pending, err := s.store.RebuiltPending(ctx)
	if err != nil {
		return false, err
	}
	if pending || s.store.Rebuilt {
		return true, nil
	}
	empty, err := s.ob.IsEmpty(ctx)
	if err != nil {
		return false, err
	}
	if !empty {
		return false, nil
	}
	n, err := s.store.CountActive(ctx)
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

// newBatchID returns a UUIDv7 string for the supersede mark's batch_id.
func newBatchID() string {
	if id, err := uuid.NewV7(); err == nil {
		return id.String()
	}
	return "reset"
}
