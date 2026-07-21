package config

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	mrand "math/rand/v2"
	"time"

	"github.com/filearr/filearr/agent/internal/outbox"
)

// Poll cadence defaults (contract §6): 300s default, 60s floor. A policy can
// retune its own cadence via poll_interval_seconds but never below the floor.
const (
	DefaultPollInterval = 300 * time.Second
	MinPollInterval     = 60 * time.Second
)

// Applier is the apply seam the daemon implements. ApplyPolicy is called with
// the honored-key view when (and only when) the applied version changes — on the
// offline-first startup seed and on each version bump the poller observes. It
// must be quick and non-blocking (live-updates in-memory daemon state); a
// returned error is logged and the version is treated as NOT applied (retried).
type Applier interface {
	ApplyPolicy(Policy) error
}

// NoopApplier applies nothing — used by the one-shot `policy --fetch` CLI where
// there is no live daemon to reconfigure (persistence still happens).
type NoopApplier struct{}

func (NoopApplier) ApplyPolicy(Policy) error { return nil }

// PollOutcome classifies a single poll for callers (CLI/tests). The Run loop
// treats OutcomeApplied and OutcomeUnchanged identically (both are healthy
// contact); only OutcomeApplied invoked the apply seam.
type PollOutcome int

const (
	// OutcomeNotModified is a 304: central confirmed the cached policy is current.
	OutcomeNotModified PollOutcome = iota
	// OutcomeUnchanged is a 200 whose (scope,version) identity matches what is
	// already applied — persisted but the apply seam was NOT invoked.
	OutcomeUnchanged
	// OutcomeApplied is a 200 with a new (scope,version) identity: applied + persisted.
	OutcomeApplied
)

func (o PollOutcome) String() string {
	switch o {
	case OutcomeNotModified:
		return "not-modified"
	case OutcomeUnchanged:
		return "unchanged"
	case OutcomeApplied:
		return "applied"
	default:
		return "unknown"
	}
}

// PollerConfig configures a Poller. The Now/Sleep/Jitter/Interval fields are
// test seams; all default to production behavior.
type PollerConfig struct {
	Client  *PolicyClient
	Cache   *ETagCache
	Applier Applier
	Logger  *slog.Logger

	// DefaultInterval / MinInterval override the poll cadence default and floor
	// (tests shrink them so a loop runs fast). Zero => the package defaults.
	DefaultInterval time.Duration
	MinInterval     time.Duration

	// Backoff shapes the network-error backoff (reused from the outbox). Zero
	// fields take the outbox defaults (1s→5m, ×2).
	Backoff outbox.BackoffConfig

	// AfterFetch, when non-nil, is invoked after every SUCCESSFUL poll (a 200 or a
	// 304 — both confirm central contact) with the policy document now in effect.
	// The daemon uses it to version-gate the taxonomy-cache refresh off the
	// policy's taxonomy_version. It runs OUTSIDE the apply-identity gate on purpose:
	// a taxonomy edit bumps only the ETag (not scope/version), so it arrives as a
	// 200 with OutcomeUnchanged and the apply seam does not fire — but AfterFetch
	// still sees the fresh doc. It must be quick/non-blocking; its own errors are
	// its concern (the poll loop ignores them).
	AfterFetch func(context.Context, PolicyDoc)

	// Test seams.
	Now    func() time.Time
	Sleep  func(context.Context, time.Duration) bool // false => ctx cancelled
	Jitter func(time.Duration) time.Duration
}

// Poller is the background policy poll loop. It owns the applied-version cursor
// and drives fetch → (apply on change) → persist, self-retuning its cadence from
// the policy and backing off on network errors (never crashing, keeping the
// last-known policy).
type Poller struct {
	client  *PolicyClient
	cache   *ETagCache
	applier Applier
	log     *slog.Logger
	backoff *outbox.Backoff

	defaultInterval time.Duration
	minInterval     time.Duration

	afterFetch func(context.Context, PolicyDoc)

	now    func() time.Time
	sleep  func(context.Context, time.Duration) bool
	jitter func(time.Duration) time.Duration

	// appliedID is the identity of the policy currently applied to the live
	// daemon — the "<scope>/<version>" ETag string, so a same-version scope FLIP
	// (e.g. global/1 → agent:<id>/1) still re-invokes the apply seam. appliedVersion
	// is the numeric version reported back via ?applied= (central stamps the number;
	// scope is implicit in central's resolution).
	appliedID      string
	appliedVersion int
}

// applyIdentity is the apply-gate identity for a policy: the ETag when central
// supplied one (exactly "<scope>/<version>"), else a synthesized equivalent so an
// older cache without an ETag still gates on the (scope,version) pair.
func applyIdentity(etag, scope string, version int) string {
	if etag != "" {
		return etag
	}
	return fmt.Sprintf("%s/%d", scope, version)
}

// NewPoller wires a Poller from cfg, applying defaults for the optional fields.
func NewPoller(cfg PollerConfig) *Poller {
	p := &Poller{
		client:          cfg.Client,
		cache:           cfg.Cache,
		applier:         cfg.Applier,
		log:             cfg.Logger,
		backoff:         outbox.NewBackoff(cfg.Backoff),
		defaultInterval: cfg.DefaultInterval,
		minInterval:     cfg.MinInterval,
		afterFetch:      cfg.AfterFetch,
		now:             cfg.Now,
		sleep:           cfg.Sleep,
		jitter:          cfg.Jitter,
	}
	if p.applier == nil {
		p.applier = NoopApplier{}
	}
	if p.log == nil {
		p.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if p.defaultInterval <= 0 {
		p.defaultInterval = DefaultPollInterval
	}
	if p.minInterval <= 0 {
		p.minInterval = MinPollInterval
	}
	if p.now == nil {
		p.now = time.Now
	}
	if p.sleep == nil {
		p.sleep = sleepCtx
	}
	if p.jitter == nil {
		p.jitter = defaultJitter
	}
	return p
}

// Run seeds from the cache (offline-first apply of the last-known policy) then
// loops: poll, apply-on-change, persist, sleep for the policy-tuned interval
// (jittered) — or back off on a network error, keeping the last-known policy.
// It returns ctx.Err() on shutdown and never returns on transient errors.
func (p *Poller) Run(ctx context.Context) error {
	p.seed(ctx)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		doc, _, err := p.pollCycle(ctx)
		var wait time.Duration
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			wait = p.backoff.Next()
			p.log.Warn("policy poll failed; keeping last-known policy", "backoff", wait.String(), "err", err)
		} else {
			p.backoff.Reset()
			wait = p.nextInterval(doc)
		}
		if !p.sleep(ctx, wait) {
			return ctx.Err()
		}
	}
}

// PollOnce performs a single fetch → apply-on-change → persist cycle (no sleep).
// It backs the `filearr-agent policy --fetch` one-shot. Returns the document in
// effect after the cycle, the outcome (not-modified / unchanged / applied), and
// any error.
func (p *Poller) PollOnce(ctx context.Context) (PolicyDoc, PollOutcome, error) {
	// A one-shot invocation has no prior in-memory cursor; seed it from the cache
	// so `applied=` reports the last persisted applied version and the apply gate
	// compares against the last cached identity.
	if doc, ok, err := p.cache.Load(); err == nil && ok {
		p.appliedVersion = doc.AppliedVersion
		p.appliedID = applyIdentity(doc.ETag, doc.Scope, doc.Version)
	}
	return p.pollCycle(ctx)
}

// seed applies the last-known cached policy to the live daemon before the first
// poll (offline-first). The seed re-applies regardless of applied_version
// because the daemon's in-memory state is fresh on every start; on success the
// applied cursor advances to the cached version.
func (p *Poller) seed(ctx context.Context) {
	doc, ok, err := p.cache.Load()
	if err != nil {
		p.log.Warn("load cached policy failed; starting with defaults", "err", err)
		return
	}
	if !ok {
		return
	}
	p.appliedVersion = doc.AppliedVersion
	pol, err := doc.Parsed()
	if err != nil {
		p.log.Warn("parse cached policy failed; starting with defaults", "err", err)
		return
	}
	if err := p.applier.ApplyPolicy(pol); err != nil {
		p.log.Error("seed apply of cached policy failed", "err", err)
		return
	}
	p.appliedVersion = doc.Version
	p.appliedID = applyIdentity(doc.ETag, doc.Scope, doc.Version)
	if doc.AppliedVersion != doc.Version {
		doc.AppliedVersion = doc.Version
		if err := p.cache.Save(doc); err != nil {
			p.log.Warn("persist seeded applied-version failed", "err", err)
		}
	}
	p.log.Info("applied cached policy (offline-first)", "scope", doc.Scope, "version", doc.Version)
}

// pollCycle fetches once and, when the (scope,version) IDENTITY changed, applies
// then persists. The gate is the identity — NOT the numeric version alone — so a
// same-version scope flip (global/1 → agent:<id>/1) still re-invokes the apply
// seam. On 304 it returns the cached doc. On an apply failure it persists the
// fetched body (so a restart retries the apply) but leaves applied_version.
func (p *Poller) pollCycle(ctx context.Context) (PolicyDoc, PollOutcome, error) {
	cur, _, _ := p.cache.Load()
	res, err := p.client.Fetch(ctx, cur.ETag, p.appliedVersion)
	if errors.Is(err, ErrNotModified) {
		// A 304 is a successful confirmation that the cache is current — advance the
		// freshness clock (P7-T4 offline-grace) so a policy that only ever 304s does
		// not drift stale, even though the body did not change. Persist it.
		cur.VerifiedAt = p.now()
		if serr := p.cache.Save(cur); serr != nil {
			p.log.Warn("persist 304 freshness failed", "err", serr)
		}
		p.fireAfterFetch(ctx, cur)
		return cur, OutcomeNotModified, nil
	}
	if err != nil {
		return cur, OutcomeNotModified, err
	}

	now := p.now()
	doc := PolicyDoc{
		ETag:           res.ETag,
		Scope:          res.Scope,
		Version:        res.Version,
		AppliedVersion: p.appliedVersion,
		FetchedAt:      now,
		VerifiedAt:     now,
		Policy:         normalizeRaw(res.Policy),
	}

	outcome := OutcomeUnchanged
	if id := applyIdentity(res.ETag, res.Scope, res.Version); id != p.appliedID {
		pol, err := ParsePolicy(res.Policy)
		if err != nil {
			return cur, OutcomeNotModified, fmt.Errorf("parse fetched policy: %w", err)
		}
		if err := p.applier.ApplyPolicy(pol); err != nil {
			// Persist the fetched body so a restart retries the apply, but keep the
			// old applied_version/identity — this policy is NOT yet applied.
			if serr := p.cache.Save(doc); serr != nil {
				p.log.Warn("persist unapplied policy failed", "err", serr)
			}
			return doc, OutcomeUnchanged, fmt.Errorf("apply policy %s: %w", id, err)
		}
		p.appliedVersion = res.Version
		p.appliedID = id
		doc.AppliedVersion = res.Version
		outcome = OutcomeApplied
		p.log.Info("applied policy", "scope", res.Scope, "version", res.Version, "identity", id)
	}

	if err := p.cache.Save(doc); err != nil {
		return doc, outcome, fmt.Errorf("persist policy: %w", err)
	}
	p.fireAfterFetch(ctx, doc)
	return doc, outcome, nil
}

// fireAfterFetch invokes the optional post-fetch hook with the doc in effect.
func (p *Poller) fireAfterFetch(ctx context.Context, doc PolicyDoc) {
	if p.afterFetch != nil {
		p.afterFetch(ctx, doc)
	}
}

// nextInterval derives the (jittered) sleep before the next poll from the doc's
// own poll_interval_seconds — a policy change can retune the cadence.
func (p *Poller) nextInterval(doc PolicyDoc) time.Duration {
	base := p.defaultInterval
	if pol, err := doc.Parsed(); err == nil {
		base = pol.PollInterval(p.defaultInterval, p.minInterval)
	}
	return p.jitter(base)
}

// AppliedVersion reports the version the poller has fully applied (test/diag).
func (p *Poller) AppliedVersion() int { return p.appliedVersion }

// defaultJitter spreads a base interval by ±10% to avoid a thundering herd of
// agents polling in lockstep.
func defaultJitter(base time.Duration) time.Duration {
	if base <= 0 {
		return base
	}
	factor := 0.9 + 0.2*mrand.Float64()
	return time.Duration(float64(base) * factor)
}

// sleepCtx sleeps for d or until ctx is cancelled; false => cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
