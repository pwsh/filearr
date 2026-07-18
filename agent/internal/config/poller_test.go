package config

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/outbox"
)

// recordingApplier captures each ApplyPolicy call (and can force an error).
type recordingApplier struct {
	mu      sync.Mutex
	applied []Policy
	err     error
}

func (a *recordingApplier) ApplyPolicy(p Policy) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.err != nil {
		return a.err
	}
	a.applied = append(a.applied, p)
	return nil
}

func (a *recordingApplier) count() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.applied)
}

// sleepDriver bounds a Poller.Run to max poll cycles by cancelling the context
// once it has slept max times, and records each requested wait.
type sleepDriver struct {
	mu     sync.Mutex
	waits  []time.Duration
	max    int
	cancel context.CancelFunc
}

func (s *sleepDriver) sleep(ctx context.Context, d time.Duration) bool {
	s.mu.Lock()
	s.waits = append(s.waits, d)
	n := len(s.waits)
	s.mu.Unlock()
	if n >= s.max {
		s.cancel()
		return false
	}
	return true
}

func (s *sleepDriver) snapshot() []time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]time.Duration, len(s.waits))
	copy(out, s.waits)
	return out
}

func identityJitter(d time.Duration) time.Duration { return d }

func TestPollerAppliesOnlyOnVersionChange(t *testing.T) {
	mc := newMockCentral("library", 5, `{"presets":["system_files"]}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	app := &recordingApplier{}
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(t.TempDir()),
		Applier: app,
		Jitter:  identityJitter,
	})

	// Cycle 1: fresh version 5 → apply.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("cycle1 outcome=%v err=%v", o, err)
	}
	// Cycle 2: 304 (same identity) → no apply.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeNotModified {
		t.Fatalf("cycle2 outcome=%v err=%v", o, err)
	}
	// Cycle 3: central bumps to 6 → apply.
	mc.set("library", 6, `{"presets":[]}`)
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("cycle3 outcome=%v err=%v", o, err)
	}

	if app.count() != 2 {
		t.Fatalf("apply seam must fire only on version change (v5, v6), got %d applies", app.count())
	}
	// applied= reporting: 0 (initial), 5 (after v5), 5 (304 kept), then 6-triggering fetch reported 5.
	if got := mc.applied(); len(got) != 3 || got[0] != 0 || got[1] != 5 || got[2] != 5 {
		t.Errorf("applied params = %v, want [0 5 5]", got)
	}
	if p.AppliedVersion() != 6 {
		t.Errorf("appliedVersion = %d, want 6", p.AppliedVersion())
	}
}

func TestPollerScopeFlipAtEqualVersionApplies(t *testing.T) {
	// E2E edge: global policy v1 is applied; admin then attaches an agent-scope
	// policy that ALSO starts at version 1. The apply gate must key on the
	// (scope,version) identity — not the number alone — so the flip re-applies.
	dir := t.TempDir()
	mc := newMockCentral("global", 1, `{"reconcile_interval_seconds":600}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	app := &recordingApplier{}
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(dir),
		Applier: app,
		Jitter:  identityJitter,
	})

	// Apply global/1.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("global/1 outcome=%v err=%v", o, err)
	}
	// Re-poll unchanged: 304, no apply.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeNotModified {
		t.Fatalf("unchanged outcome=%v err=%v", o, err)
	}
	// Scope flip to agent:a/1 — SAME version number, different scope.
	mc.set("agent:a", 1, `{"reconcile_interval_seconds":30}`)
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("scope-flip must APPLY (identity changed), got outcome=%v err=%v", o, err)
	}
	// And a further re-poll of the same agent-scope policy is a 304 no-apply.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeNotModified {
		t.Fatalf("post-flip unchanged outcome=%v err=%v", o, err)
	}

	if app.count() != 2 {
		t.Fatalf("apply seam must fire exactly twice (global/1, agent:a/1), got %d", app.count())
	}

	// applied_version tracking survives the flip: still version 1, but the applied
	// IDENTITY is now the agent scope, and the persisted body is the agent one.
	doc, ok, err := NewETagCache(dir).Load()
	if err != nil || !ok {
		t.Fatalf("reload: ok=%v err=%v", ok, err)
	}
	if doc.Scope != "agent:a" || doc.Version != 1 || doc.AppliedVersion != 1 {
		t.Errorf("post-flip cache = scope=%s version=%d applied=%d, want agent:a/1 applied=1",
			doc.Scope, doc.Version, doc.AppliedVersion)
	}
	if p.AppliedVersion() != 1 {
		t.Errorf("applied version = %d, want 1", p.AppliedVersion())
	}
}

func TestPollerAppliedReportingAdvances(t *testing.T) {
	mc := newMockCentral("library", 5, `{}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(t.TempDir()),
		Applier: &recordingApplier{},
		Jitter:  identityJitter,
	})
	_, _, _ = p.PollOnce(context.Background()) // applied=0, apply v5
	mc.set("library", 9, `{}`)
	_, _, _ = p.PollOnce(context.Background()) // applied=5 reported, apply v9
	_, _, _ = p.PollOnce(context.Background()) // applied=9 reported

	got := mc.applied()
	if len(got) != 3 || got[0] != 0 || got[1] != 5 || got[2] != 9 {
		t.Fatalf("applied= must advance after each apply: %v, want [0 5 9]", got)
	}
}

func TestPollerOfflineStartupFromCache(t *testing.T) {
	// Pre-seed the cache as if a prior run persisted a policy, then start the
	// poller against a DOWN server: seed must apply the cached policy with no
	// network, and the loop must keep it despite the fetch error.
	dir := t.TempDir()
	cache := NewETagCache(dir)
	if err := cache.Save(PolicyDoc{
		ETag: `"library/4"`, Scope: "library", Version: 4, AppliedVersion: 4,
		Policy: []byte(`{"presets":["caches_temp"],"reconcile_interval_seconds":600}`),
	}); err != nil {
		t.Fatal(err)
	}

	mc := newMockCentral("library", 4, `{}`)
	mc.status = http.StatusServiceUnavailable // network/central down
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	app := &recordingApplier{}
	ctx, cancel := context.WithCancel(context.Background())
	drv := &sleepDriver{max: 1, cancel: cancel}
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   cache,
		Applier: app,
		Jitter:  identityJitter,
		Sleep:   drv.sleep,
	})
	_ = p.Run(ctx)

	if app.count() != 1 {
		t.Fatalf("offline startup must apply the cached policy once (seed), got %d", app.count())
	}
	app.mu.Lock()
	seeded := app.applied[0]
	app.mu.Unlock()
	if len(seeded.Presets) != 1 || seeded.Presets[0] != "caches_temp" {
		t.Errorf("seed applied wrong policy: %+v", seeded)
	}
	// The failed fetch backed off (non-default wait) and the cached doc survived.
	if _, ok, _ := cache.Load(); !ok {
		t.Error("cached policy must survive a network error")
	}
}

func TestPollerSelfRetunesInterval(t *testing.T) {
	// Policy sets a 120s poll interval; the next sleep must honor it (jitter off).
	mc := newMockCentral("library", 1, `{"poll_interval_seconds":120}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	drv := &sleepDriver{max: 1, cancel: cancel}
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(t.TempDir()),
		Applier: &recordingApplier{},
		Jitter:  identityJitter,
		Sleep:   drv.sleep,
	})
	_ = p.Run(ctx)

	waits := drv.snapshot()
	if len(waits) != 1 || waits[0] != 120*time.Second {
		t.Fatalf("poll interval must self-retune to 120s, got %v", waits)
	}
}

func TestPollerBacksOffOnNetworkError(t *testing.T) {
	mc := newMockCentral("library", 1, `{}`)
	mc.status = http.StatusServiceUnavailable
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	app := &recordingApplier{}
	ctx, cancel := context.WithCancel(context.Background())
	drv := &sleepDriver{max: 3, cancel: cancel}
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(t.TempDir()),
		Applier: app,
		Jitter:  identityJitter,
		Sleep:   drv.sleep,
		Backoff: outbox.BackoffConfig{Min: 10 * time.Millisecond, Max: time.Second, Factor: 2},
	})
	_ = p.Run(ctx)

	waits := drv.snapshot()
	if len(waits) != 3 {
		t.Fatalf("expected 3 backoff cycles, got %v", waits)
	}
	// Capped exponential backoff: strictly increasing until the cap.
	if !(waits[0] < waits[1] && waits[1] < waits[2]) {
		t.Errorf("backoff must grow on repeated errors, got %v", waits)
	}
	if app.count() != 0 {
		t.Error("no apply must happen while central is unreachable")
	}
}

func TestPollerPersistsAcrossRestart(t *testing.T) {
	// A fetched+applied policy must be readable by a fresh poller (persistence).
	dir := t.TempDir()
	mc := newMockCentral("library", 2, `{"presets":["node_modules_build"]}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	p1 := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   NewETagCache(dir),
		Applier: &recordingApplier{},
		Jitter:  identityJitter,
	})
	if _, o, err := p1.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("first poll outcome=%v err=%v", o, err)
	}

	// A brand-new poller (fresh process) seeds from the persisted cache.
	doc, ok, err := NewETagCache(dir).Load()
	if err != nil || !ok {
		t.Fatalf("reload: ok=%v err=%v", ok, err)
	}
	if doc.Version != 2 || doc.AppliedVersion != 2 {
		t.Errorf("persisted version/applied = %d/%d, want 2/2", doc.Version, doc.AppliedVersion)
	}
	pol, _ := doc.Parsed()
	if len(pol.Presets) != 1 || pol.Presets[0] != "node_modules_build" {
		t.Errorf("persisted policy body wrong: %+v", pol)
	}
}
