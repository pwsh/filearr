package reconcile

import (
	"context"
	"sync"
	"testing"
	"time"
)

// recordingSweep is a fake SweepFunc capturing each invocation's ForceReset.
type recordingSweep struct {
	mu    sync.Mutex
	calls []bool
	fired chan struct{}
}

func newRecordingSweep() *recordingSweep {
	return &recordingSweep{fired: make(chan struct{}, 64)}
}

func (r *recordingSweep) fn(_ context.Context, opts Options) (SweepResult, error) {
	r.mu.Lock()
	r.calls = append(r.calls, opts.ForceReset)
	r.mu.Unlock()
	r.fired <- struct{}{}
	return SweepResult{}, nil
}

func (r *recordingSweep) snapshot() []bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]bool, len(r.calls))
	copy(out, r.calls)
	return out
}

// waitCall blocks until at least one sweep has fired or the timeout elapses.
func waitCall(t *testing.T, r *recordingSweep, timeout time.Duration) {
	t.Helper()
	select {
	case <-r.fired:
	case <-time.After(timeout):
		t.Fatal("expected a sweep to fire but none did")
	}
}

func TestSupervisorCursorDeadEndForcesResetSweep(t *testing.T) {
	rec := newRecordingSweep()
	sup := NewSupervisor(rec.fn, 0, time.Hour, nil) // interval off; only the trigger fires
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sup.Run(ctx)

	sup.CursorDeadEnd() // Observer hook from the replicator dead-end
	waitCall(t, rec, time.Second)

	calls := rec.snapshot()
	if len(calls) == 0 || !calls[0] {
		t.Fatalf("cursor dead-end must schedule a ForceReset sweep, got %v", calls)
	}
}

func TestSupervisorIntervalTriggersSweep(t *testing.T) {
	rec := newRecordingSweep()
	sup := NewSupervisor(rec.fn, 15*time.Millisecond, time.Hour, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sup.Run(ctx)

	waitCall(t, rec, time.Second) // the periodic tick alone must drive a sweep
	if calls := rec.snapshot(); calls[0] {
		t.Error("an interval-triggered sweep must NOT force a reset")
	}
}

func TestSupervisorReconnectGatedByThreshold(t *testing.T) {
	rec := newRecordingSweep()
	sup := NewSupervisor(rec.fn, 0, 100*time.Millisecond, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sup.Run(ctx)

	// A brief blip below the threshold must NOT sweep.
	sup.Reconnected(10 * time.Millisecond)
	select {
	case <-rec.fired:
		t.Fatal("a short outage must not trigger a reconcile")
	case <-time.After(80 * time.Millisecond):
	}

	// A long outage above the threshold must sweep (non-forced).
	sup.Reconnected(5 * time.Second)
	waitCall(t, rec, time.Second)
	calls := rec.snapshot()
	if len(calls) != 1 || calls[0] {
		t.Fatalf("long-outage reconnect must trigger exactly one non-reset sweep, got %v", calls)
	}
}

func TestSupervisorCoalescesPreservingReset(t *testing.T) {
	// Multiple triggers queued before the worker picks up collapse into one run;
	// if ANY was a forced reset, the coalesced run is a reset. Block the sweep on a
	// gate so several triggers land while one is pending.
	release := make(chan struct{})
	var mu sync.Mutex
	var forced []bool
	fired := make(chan struct{}, 8)
	sweep := func(_ context.Context, opts Options) (SweepResult, error) {
		mu.Lock()
		forced = append(forced, opts.ForceReset)
		mu.Unlock()
		fired <- struct{}{}
		<-release
		return SweepResult{}, nil
	}
	sup := NewSupervisor(sweep, 0, time.Hour, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sup.Run(ctx)

	sup.Trigger(false) // first run starts and blocks on release
	<-fired
	// While the first run is in flight, queue a non-reset then a reset trigger.
	sup.Trigger(false)
	sup.Trigger(true)
	close(release) // let runs proceed

	<-fired // the coalesced second run
	mu.Lock()
	defer mu.Unlock()
	if len(forced) < 2 {
		t.Fatalf("expected at least 2 runs, got %d", len(forced))
	}
	if !forced[len(forced)-1] {
		t.Error("a coalesced run that included a reset trigger must force a reset")
	}
}
