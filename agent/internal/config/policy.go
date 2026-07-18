// Package config implements the P5-T6 agent policy poll client: the reliable
// background path that fetches the agent's central policy via
// GET /api/v1/agents/{id}/policy with ETag/If-None-Match, persists it
// offline-first, and applies it to the running daemon (reconcile cadence, scan
// settings, watch-mode gating). It is the client half of the frozen contract in
// docs/research/phase-5-distributed-agents.md §6; the opportunistic SSE
// near-instant path (layout.md) is deferred.
//
// Contract invariant: UNKNOWN policy keys must round-trip byte-for-byte. The
// persisted document stores the policy body as a raw json.RawMessage and never
// re-marshals it through the typed Policy view below — the typed view is used
// only to READ the keys the agent honors.
package config

import (
	"encoding/json"
	"sort"
	"time"
)

// DefaultOfflineGrace is the offline-grace default for the local query surface
// (P7-T4 / research §5.2, ruling R4). It REUSES Phase-5's 24h reconciliation
// threshold — this is the SAME value cmd's defaultReconcileInterval carries
// (that constant is defined as this one), NOT a second constant. Past this window
// with no fresh policy, the web UI fails closed while the CLI same-user path keeps
// answering; a policy may override the window via offline_grace_seconds.
const DefaultOfflineGrace = 24 * time.Hour

// Policy is the typed VIEW of the v1 policy keys the agent honors. Every field
// is optional; a nil/absent field means "keep the agent's local/default value"
// (the contract's absent-key semantics). Scalars are pointers so an explicitly
// present value is distinguishable from an absent one. This struct is used only
// to read a policy — persistence always round-trips the raw JSON (see PolicyDoc)
// so unknown keys survive.
type Policy struct {
	Presets                  []string `json:"presets"`
	IncludeGlobs             []string `json:"include_globs"`
	ExcludeGlobs             []string `json:"exclude_globs"`
	ContentHashMaxBytes      *int64   `json:"content_hash_max_bytes"`
	WatchMode                *bool    `json:"watch_mode"`
	ReconcileIntervalSeconds *int     `json:"reconcile_interval_seconds"`
	PollIntervalSeconds      *int     `json:"poll_interval_seconds"`

	// P7-T4 local query surface keys. Absent (nil) → the never-contacted default
	// baked into the accessors below (CLI on, web UI off, auth required, read-only).
	LocalAccessEnabled  *bool    `json:"local_access_enabled"`
	WebUIEnabled        *bool    `json:"web_ui_enabled"`
	AuthRequired        *bool    `json:"auth_required"`
	ReadOnly            *bool    `json:"read_only"`
	PathScope           []string `json:"path_scope"`
	OfflineGraceSeconds *int     `json:"offline_grace_seconds"`

	// P10-T4 agent staging-upload rate cap (bytes/sec). Absent (nil) or 0 =>
	// UNLIMITED. Read at upload START; a mid-upload change applies on the next
	// upload (documented). Additive — does not disturb the P7-T4 keys above.
	UploadRatePerSec *int64 `json:"upload_rate_bytes_per_sec"`
}

// UploadRateBytesPerSec is the per-agent staging-upload token-bucket ceiling in
// bytes/sec (P10-T4, research §2.4). The never-contacted / absent / zero /
// negative value is 0 == UNLIMITED (no throttle). The command poller reads this
// from the cached policy at the START of each stage_upload and sizes the
// executor's token bucket from it; a change mid-upload applies on the next one.
func (p Policy) UploadRateBytesPerSec() int64 {
	if p.UploadRatePerSec == nil || *p.UploadRatePerSec < 0 {
		return 0
	}
	return *p.UploadRatePerSec
}

// LocalAccessAllowed reports whether the CLI/local-API listener may answer. The
// never-contacted default is TRUE (CLI default-on, research §5.2); only an
// explicit local_access_enabled=false disables it — and because that value is
// cached, the disable persists through offline periods. Freshness NEVER gates the
// CLI (its peer-credential check is offline-capable — R4 asymmetry).
func (p Policy) LocalAccessAllowed() bool {
	if p.LocalAccessEnabled == nil {
		return true
	}
	return *p.LocalAccessEnabled
}

// WebUIRequested reports the policy's web_ui_enabled value; the never-contacted
// default is FALSE (a fresh agent starts web-UI-disabled, research §5.2). This is
// the RAW policy intent — the effective capability additionally requires a fresh
// policy (see PolicyDoc.WebUIAllowed / the stale rule).
func (p Policy) WebUIRequested() bool {
	if p.WebUIEnabled == nil {
		return false
	}
	return *p.WebUIEnabled
}

// AuthRequiredValue reports whether the web UI must demand its bootstrap token;
// the never-contacted default is TRUE (auth on by default). Never affects the CLI
// peer-credential check.
func (p Policy) AuthRequiredValue() bool {
	if p.AuthRequired == nil {
		return true
	}
	return *p.AuthRequired
}

// PathScopePredicates returns the flattened rel_path GLOB allow-list the agent
// applies to every local query (R2 — flattened predicates only, no rule
// evaluator). Empty = unrestricted. Returns a copy so callers cannot mutate the
// parsed policy's slice.
func (p Policy) PathScopePredicates() []string {
	if len(p.PathScope) == 0 {
		return nil
	}
	out := make([]string, len(p.PathScope))
	copy(out, p.PathScope)
	return out
}

// OfflineGrace resolves the web-UI fail-closed grace window: the policy's
// offline_grace_seconds when present and non-negative, else def (which callers
// pass as DefaultOfflineGrace). A zero policy value means "fail closed
// immediately when offline" and is honored verbatim.
func (p Policy) OfflineGrace(def time.Duration) time.Duration {
	if p.OfflineGraceSeconds == nil || *p.OfflineGraceSeconds < 0 {
		return def
	}
	return time.Duration(*p.OfflineGraceSeconds) * time.Second
}

// ParsePolicy decodes the honored-key view from a raw policy body. An empty body
// (nil or "{}") yields a zero Policy (all-absent → all-default). Unknown keys in
// raw are silently ignored here (they are preserved separately via PolicyDoc).
func ParsePolicy(raw json.RawMessage) (Policy, error) {
	var p Policy
	if len(raw) == 0 {
		return p, nil
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return Policy{}, err
	}
	return p, nil
}

// PollInterval resolves the poll cadence from the policy: the policy's
// poll_interval_seconds when present, else def; never below floor (a policy can
// retune its own cadence but not below the safety floor).
func (p Policy) PollInterval(def, floor time.Duration) time.Duration {
	if p.PollIntervalSeconds == nil {
		return def
	}
	d := time.Duration(*p.PollIntervalSeconds) * time.Second
	if d < floor {
		return floor
	}
	return d
}

// ReconcileInterval returns the policy's reconcile cadence and true when the
// policy sets a positive reconcile_interval_seconds; (0,false) means "absent —
// keep the daemon's current interval".
func (p Policy) ReconcileInterval() (time.Duration, bool) {
	if p.ReconcileIntervalSeconds == nil || *p.ReconcileIntervalSeconds <= 0 {
		return 0, false
	}
	return time.Duration(*p.ReconcileIntervalSeconds) * time.Second, true
}

// WatchAllowed reports the policy's watch_mode: allowed is its value, set is
// whether the policy specifies it at all. When set is false the caller keeps its
// local watch decision (absent = keep local).
func (p Policy) WatchAllowed() (allowed, set bool) {
	if p.WatchMode == nil {
		return false, false
	}
	return *p.WatchMode, true
}

// ScanSettings is the scan-relevant slice of a scan configuration that central
// policy can override. It mirrors the cmd scanConfig's indexing knobs; the cmd
// scan path builds one from scan.json, overlays the cached policy, and feeds the
// result to the next scan.
type ScanSettings struct {
	Presets             []string
	IncludeGlobs        []string
	ExcludeGlobs        []string
	ContentCeilingBytes int64
}

// OverlayScan returns s with the policy-provided keys taking precedence (policy
// wins). Only keys the policy actually specifies override; absent keys keep s.
// This is the documented precedence: central policy overrides the agent's local
// scan.json for the keys it sets, leaving the rest local.
func (p Policy) OverlayScan(s ScanSettings) ScanSettings {
	if p.Presets != nil {
		s.Presets = p.Presets
	}
	if p.IncludeGlobs != nil {
		s.IncludeGlobs = p.IncludeGlobs
	}
	if p.ExcludeGlobs != nil {
		s.ExcludeGlobs = p.ExcludeGlobs
	}
	if p.ContentHashMaxBytes != nil {
		s.ContentCeilingBytes = *p.ContentHashMaxBytes
	}
	return s
}

// PolicyDoc is the on-disk persisted policy record (<DataDir>/policy.json). It
// carries the caching metadata plus the policy body as a RAW message so unknown
// keys survive a persist→reload→re-serialize cycle byte-for-byte.
type PolicyDoc struct {
	ETag           string    `json:"etag"`
	Scope          string    `json:"scope"`
	Version        int       `json:"version"`
	AppliedVersion int       `json:"applied_version"`
	FetchedAt      time.Time `json:"fetched_at"`
	// VerifiedAt is the last time central CONFIRMED this cache is current — set on
	// a 200 (fresh body) AND a 304 (cache still current). It is the freshness
	// clock for the P7-T4 offline-grace / web-UI fail-closed rule (a 304 keeps the
	// cache fresh even though the body did not change). Absent (zero) in caches
	// written before P7-T4 → freshness falls back to FetchedAt.
	VerifiedAt time.Time       `json:"verified_at"`
	Policy     json.RawMessage `json:"policy"`
}

// Parsed decodes the honored-key view of the stored policy body.
func (d PolicyDoc) Parsed() (Policy, error) { return ParsePolicy(d.Policy) }

// freshnessAt is the effective last-confirmed-current time: VerifiedAt when set,
// else FetchedAt (backward compatibility with pre-P7-T4 caches).
func (d PolicyDoc) freshnessAt() time.Time {
	if !d.VerifiedAt.IsZero() {
		return d.VerifiedAt
	}
	return d.FetchedAt
}

// GraceExpiresAt is when the cached policy goes stale: last-confirmed-current +
// the (policy-tuned) grace window. def is the caller-supplied default window
// (DefaultOfflineGrace). A zero freshness clock (never successfully contacted)
// yields the zero time — treat as already stale (see Stale).
func (d PolicyDoc) GraceExpiresAt(def time.Duration) time.Time {
	f := d.freshnessAt()
	if f.IsZero() {
		return time.Time{}
	}
	pol, _ := d.Parsed()
	return f.Add(pol.OfflineGrace(def))
}

// Stale reports whether the cached policy is past its offline-grace window at
// now. A cache that was never confirmed current (zero freshness clock) is stale.
func (d PolicyDoc) Stale(now time.Time, def time.Duration) bool {
	f := d.freshnessAt()
	if f.IsZero() {
		return true
	}
	return now.After(d.GraceExpiresAt(def))
}

// WebUIAllowed is the EFFECTIVE local-web-UI capability P7-T5 will consume: the
// policy asked for it (web_ui_enabled=true) AND the cached policy is still fresh
// (not past offline grace). This is the asymmetric fail-closed rule (research
// §5.2 / R4): the web UI auto-disables when the policy goes stale, with no
// central push. A never-contacted agent has no web_ui_enabled=true → false.
func (d PolicyDoc) WebUIAllowed(now time.Time, def time.Duration) bool {
	pol, err := d.Parsed()
	if err != nil {
		return false // a malformed cache fails closed for the web UI
	}
	return pol.WebUIRequested() && !d.Stale(now, def)
}

// LocalSurface is the resolved local-query-surface posture derived from a cached
// policy document at a point in time — the single computation the daemon's
// localapi gate consumes (mapped onto localapi.PolicyView by the cmd wiring).
type LocalSurface struct {
	// LocalAccessEnabled gates the CLI/local-API listener (persists an explicit
	// disable through offline; never freshness-gated).
	LocalAccessEnabled bool
	// WebUIEnabled is the EFFECTIVE web-UI capability (policy intent AND fresh).
	WebUIEnabled bool
	// AuthRequired gates the web UI bootstrap-token exchange.
	AuthRequired bool
	// Predicates is the flattened path-scope allow-list, applied stale or fresh
	// (the most-restrictive last-known scope is simply the cached one — R2/§4.4).
	Predicates []string
	// Stale is true once the cache is past its offline-grace window.
	Stale bool
	// GraceExpiresAt is when the cache goes stale (zero if never contacted).
	GraceExpiresAt time.Time
	// Version is the cached policy version (for the health probe).
	Version int
}

// LocalSurface resolves the local-query-surface posture for now using def as the
// offline-grace default. It is the authoritative agent-side interpretation of the
// P7-T4 keys: CLI default-on, web UI default-off + fail-closed-when-stale, scope
// applied verbatim (stale or fresh).
func (d PolicyDoc) LocalSurface(now time.Time, def time.Duration) LocalSurface {
	pol, _ := d.Parsed()
	return LocalSurface{
		LocalAccessEnabled: pol.LocalAccessAllowed(),
		WebUIEnabled:       d.WebUIAllowed(now, def),
		AuthRequired:       pol.AuthRequiredValue(),
		Predicates:         pol.PathScopePredicates(),
		Stale:              d.Stale(now, def),
		GraceExpiresAt:     d.GraceExpiresAt(def),
		Version:            d.Version,
	}
}

// PolicyKeys returns the sorted top-level keys present in the raw policy body
// (including unknown ones) — used by the `policy` CLI to show what central set.
func (d PolicyDoc) PolicyKeys() []string {
	if len(d.Policy) == 0 {
		return nil
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(d.Policy, &m); err != nil {
		return nil
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// normalizeRaw coerces an empty/absent policy body to an empty object so the
// persisted document always holds valid JSON.
func normalizeRaw(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("{}")
	}
	return raw
}
