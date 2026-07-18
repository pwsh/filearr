package config

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"testing"
	"time"
)

// --- never-contacted defaults (accessors) ---------------------------------

func TestLocalAccessDefaultsForNeverContacted(t *testing.T) {
	p := Policy{} // absent everything
	if !p.LocalAccessAllowed() {
		t.Error("CLI must default ENABLED for a never-contacted agent")
	}
	if p.WebUIRequested() {
		t.Error("web UI must default DISABLED for a never-contacted agent")
	}
	if !p.AuthRequiredValue() {
		t.Error("auth must default REQUIRED")
	}
	if len(p.PathScopePredicates()) != 0 {
		t.Error("scope must default empty (unrestricted)")
	}
	if got := p.OfflineGrace(DefaultOfflineGrace); got != DefaultOfflineGrace {
		t.Errorf("absent offline_grace must use the 24h default, got %s", got)
	}
}

func TestExplicitDisablesHonored(t *testing.T) {
	f := false
	p := Policy{LocalAccessEnabled: &f}
	if p.LocalAccessAllowed() {
		t.Error("explicit local_access_enabled=false must disable the CLI gate")
	}
}

func TestOfflineGracePolicyOverride(t *testing.T) {
	secs := 3600
	if got := (Policy{OfflineGraceSeconds: &secs}).OfflineGrace(DefaultOfflineGrace); got != time.Hour {
		t.Errorf("policy offline_grace_seconds must win, got %s", got)
	}
	zero := 0
	if got := (Policy{OfflineGraceSeconds: &zero}).OfflineGrace(DefaultOfflineGrace); got != 0 {
		t.Errorf("offline_grace_seconds=0 (fail-immediately) must be honored, got %s", got)
	}
}

// --- freshness / asymmetric fail-closed on a cached doc -------------------

func mkDoc(policyJSON string, verifiedAgo time.Duration, now time.Time) PolicyDoc {
	return PolicyDoc{
		Scope: "global", Version: 1,
		FetchedAt:  now.Add(-verifiedAgo),
		VerifiedAt: now.Add(-verifiedAgo),
		Policy:     json.RawMessage(policyJSON),
	}
}

func TestWebUIEnabledAndFreshIsAllowed(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	d := mkDoc(`{"web_ui_enabled":true}`, time.Hour, now)
	if d.Stale(now, DefaultOfflineGrace) {
		t.Fatal("1h-old cache must be fresh under 24h grace")
	}
	if !d.WebUIAllowed(now, DefaultOfflineGrace) {
		t.Fatal("fresh web_ui_enabled must be allowed")
	}
}

func TestWebUIEnabledButStaleAutoDisables(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	// last policy said web UI enabled, but it is 100h old (> 24h grace) and has NOT
	// been refreshed — it must auto-disable the web UI with NO central push.
	d := mkDoc(`{"web_ui_enabled":true}`, 100*time.Hour, now)
	if !d.Stale(now, DefaultOfflineGrace) {
		t.Fatal("100h-old cache must be stale under 24h grace")
	}
	if d.WebUIAllowed(now, DefaultOfflineGrace) {
		t.Fatal("stale web UI must fail closed (auto-disable) with no central push")
	}
	// ...while the CLI same-user path KEEPS answering (asymmetry, R4).
	ls := d.LocalSurface(now, DefaultOfflineGrace)
	if !ls.LocalAccessEnabled {
		t.Fatal("CLI must keep answering even when the cache is stale")
	}
	if ls.WebUIEnabled {
		t.Fatal("effective web UI must be off when stale")
	}
}

func TestExplicitCLIDisablePersistsThroughStaleness(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	// An explicit disable persists even long past grace (it is cached, not
	// freshness-gated).
	d := mkDoc(`{"local_access_enabled":false}`, 1000*time.Hour, now)
	ls := d.LocalSurface(now, DefaultOfflineGrace)
	if ls.LocalAccessEnabled {
		t.Fatal("an explicit local_access_enabled=false must persist through offline")
	}
}

func TestOfflineGraceOverrideTightensWebUIWindow(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	// A 1h grace override makes a 2h-old cache stale even though the 24h default
	// would have kept it fresh.
	d := mkDoc(`{"web_ui_enabled":true,"offline_grace_seconds":3600}`, 2*time.Hour, now)
	if !d.Stale(now, DefaultOfflineGrace) {
		t.Fatal("policy offline_grace override must tighten the window")
	}
	if d.WebUIAllowed(now, DefaultOfflineGrace) {
		t.Fatal("web UI must fail closed past the overridden grace")
	}
}

func TestStaleScopeKeptMostRestrictive(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	// The last-known scope is enforced even when the cache is stale — NEVER widened
	// to unrestricted (research §4.4, most-restrictive last-known).
	d := mkDoc(`{"path_scope":["Movies/**"],"web_ui_enabled":true}`, 100*time.Hour, now)
	ls := d.LocalSurface(now, DefaultOfflineGrace)
	if !ls.Stale {
		t.Fatal("cache must be stale")
	}
	if len(ls.Predicates) != 1 || ls.Predicates[0] != "Movies/**" {
		t.Fatalf("stale cache must keep enforcing the last-known scope, got %v", ls.Predicates)
	}
}

// --- 304 advances the freshness clock (persists through offline) ----------

func TestPollerAdvancesFreshnessOn304(t *testing.T) {
	dir := t.TempDir()
	mc := newMockCentral("global", 1, `{"web_ui_enabled":true}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	clock := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	cache := NewETagCache(dir)
	p := NewPoller(PollerConfig{
		Client:  NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"}),
		Cache:   cache,
		Applier: &recordingApplier{},
		Jitter:  identityJitter,
		Now:     func() time.Time { return clock },
	})

	// 200 fetch stamps VerifiedAt at the current clock.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("first poll outcome=%v err=%v", o, err)
	}
	doc, _, _ := cache.Load()
	if !doc.VerifiedAt.Equal(clock) {
		t.Fatalf("200 must stamp VerifiedAt=%v, got %v", clock, doc.VerifiedAt)
	}

	// Advance well past the 24h grace, then re-poll → 304 (body unchanged). The 304
	// is a successful confirmation and MUST advance the freshness clock so the web
	// UI does not drift stale on a policy that only ever 304s.
	clock = clock.Add(48 * time.Hour)
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeNotModified {
		t.Fatalf("expected 304, got outcome=%v err=%v", o, err)
	}
	doc, _, _ = cache.Load()
	if !doc.VerifiedAt.Equal(clock) {
		t.Fatalf("304 must advance VerifiedAt to %v, got %v", clock, doc.VerifiedAt)
	}
	if doc.Stale(clock, DefaultOfflineGrace) {
		t.Fatal("a 304 refresh must clear staleness")
	}
	if !doc.WebUIAllowed(clock, DefaultOfflineGrace) {
		t.Fatal("web UI must be allowed again after a 304 refresh")
	}
}
