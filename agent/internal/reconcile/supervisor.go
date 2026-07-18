package reconcile

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"time"
)

// SweepFunc runs one sweep. The Supervisor is decoupled from *Sweeper through
// this seam so its trigger wiring is unit-testable with a fake.
type SweepFunc func(ctx context.Context, opts Options) (SweepResult, error)

// Supervisor centralizes the reconcile TRIGGERS for the `run` daemon and
// serializes their execution (single-flight, coalescing). It satisfies
// outbox.Observer so the replication drain can drive triggers (b) and (c):
//
//	(a) slow interval        — every Interval, a plain sweep.
//	(b) reconnect after long  — Reconnected(downFor); sweeps iff downFor > DownThreshold.
//	    outage
//	(c) replication dead-end  — CursorDeadEnd(); a forced reset_seq sweep.
//
// Trigger (d), the manual `filearr-agent reconcile` one-shot, does not go through
// the Supervisor — it calls Sweeper.Sweep directly.
type Supervisor struct {
	sweep         SweepFunc
	interval      time.Duration
	downThreshold time.Duration
	log           *slog.Logger

	mu           sync.Mutex
	pending      bool
	pendingReset bool
	wake         chan struct{}
	retick       chan struct{} // signals Run to rebuild its ticker after SetInterval
}

// NewSupervisor builds a Supervisor. A non-positive interval disables the periodic
// tick (triggers b/c still work). downThreshold gates trigger (b).
func NewSupervisor(sweep SweepFunc, interval, downThreshold time.Duration, log *slog.Logger) *Supervisor {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &Supervisor{
		sweep:         sweep,
		interval:      interval,
		downThreshold: downThreshold,
		log:           log,
		wake:          make(chan struct{}, 1),
		retick:        make(chan struct{}, 1),
	}
}

// SetInterval live-updates the periodic-tick cadence (trigger a). It is how the
// P5-T6 policy poller retunes the reconcile interval without a restart: the
// running loop rebuilds its ticker on the next iteration. A non-positive d
// disables the periodic tick (triggers b/c still fire). Safe to call from any
// goroutine; a no-op when the interval is unchanged.
func (s *Supervisor) SetInterval(d time.Duration) {
	s.mu.Lock()
	if d == s.interval {
		s.mu.Unlock()
		return
	}
	s.interval = d
	s.mu.Unlock()
	select {
	case s.retick <- struct{}{}:
	default: // a retick is already queued; Run will read the latest interval
	}
}

// currentInterval reads the (mutable) interval under the lock.
func (s *Supervisor) currentInterval() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.interval
}

// CursorDeadEnd implements outbox.Observer: the replication cursor cannot advance,
// so request a forced reset_seq sweep (the repair path).
func (s *Supervisor) CursorDeadEnd() {
	s.log.Warn("replication cursor dead-end; scheduling reset reconcile")
	s.Trigger(true)
}

// Reconnected implements outbox.Observer: after the drain recovers from an outage
// longer than DownThreshold, central's view may be stale — request a sweep.
func (s *Supervisor) Reconnected(downFor time.Duration) {
	if s.downThreshold > 0 && downFor <= s.downThreshold {
		return
	}
	s.log.Info("replication reconnected after outage; scheduling reconcile", "down_for", downFor.String())
	s.Trigger(false)
}

// Trigger schedules a sweep. Calls before the worker picks up coalesce into one
// run; if ANY coalesced request forced a reset, the run is a reset sweep.
func (s *Supervisor) Trigger(forceReset bool) {
	s.mu.Lock()
	s.pending = true
	if forceReset {
		s.pendingReset = true
	}
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default: // a wake is already queued; the worker will read the coalesced state
	}
}

// Run drives the periodic tick and executes scheduled sweeps serially until ctx
// is cancelled. It returns ctx.Err() on shutdown.
func (s *Supervisor) Run(ctx context.Context) error {
	var ticker *time.Ticker
	var tickC <-chan time.Time
	arm := func() {
		if ticker != nil {
			ticker.Stop()
			ticker = nil
			tickC = nil
		}
		if iv := s.currentInterval(); iv > 0 {
			ticker = time.NewTicker(iv)
			tickC = ticker.C
		}
	}
	arm()
	defer func() {
		if ticker != nil {
			ticker.Stop()
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.retick:
			arm() // SetInterval changed the cadence; rebuild the ticker
		case <-tickC:
			s.log.Debug("reconcile interval elapsed; scheduling sweep")
			s.Trigger(false)
		case <-s.wake:
			s.mu.Lock()
			if !s.pending {
				s.mu.Unlock()
				continue
			}
			reset := s.pendingReset
			s.pending, s.pendingReset = false, false
			s.mu.Unlock()
			s.runOnce(ctx, reset)
		}
	}
}

// runOnce executes a single sweep and logs the outcome; errors never crash the
// daemon (the next trigger retries).
func (s *Supervisor) runOnce(ctx context.Context, forceReset bool) {
	res, err := s.sweep(ctx, Options{ForceReset: forceReset})
	if err != nil {
		s.log.Error("reconcile sweep failed", "force_reset", forceReset, "err", err)
	}
	for _, rr := range res.Roots {
		if rr.Err != nil {
			s.log.Error("reconcile root failed", "library_ref", rr.LibraryRef, "err", rr.Err)
			continue
		}
		if rr.Matched {
			s.log.Info("reconcile root matched", "library_ref", rr.LibraryRef, "rows", rr.RowCount)
			continue
		}
		s.log.Info("reconcile root reconciled",
			"library_ref", rr.LibraryRef, "rows", rr.RowCount, "reset", rr.Reset,
			"counters", rr.Finish.SortedCounters())
	}
	if res.Reset {
		s.log.Info("reconcile reset epilogue", "outbox_marked", res.OutboxMarked)
	}
}
