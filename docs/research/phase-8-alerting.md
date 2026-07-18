# Research Brief — Future Roadmap Item 6: Alerting

Scope: `docs/future-roadmap.md` §6 (Alerting). File-change alert rules (path
glob + event type created/modified/deleted/moved + optional hash-change) →
notification channels (webhook/email/Apprise), per-rule throttling/digests,
local evaluation on offline agents with delivery on reconnect, plus
operational alerts (scan failures, agent offline, replication lag,
extract-error spikes — extends Phase-1 T11). Constraint order for every
tradeoff below: **security > integrity > reliability > speed > compatibility
> scalability**. No Redis. AGPL-3.0-or-later-compatible OSS dependencies
only.

Research current as of **2026-07-07**. Where a claim rests on search
snippets rather than a fully-verified primary source, it is flagged inline.

---

## 1. Notification dispatch: Apprise vs ntfy vs Shoutrrr vs hand-rolled

### 1.1 Apprise (`caronc/apprise`, PyPI `apprise`)

- **License: MIT**, confirmed. The project *was* GPLv3 in its early history
  and the maintainer deliberately relicensed to MIT (github.com/caronc/apprise
  issue #47, "Change Project Licensing over to MIT") — fully AGPL-compatible,
  no copyleft obligations flow onto Filearr.
- **Maintenance**: active — repo shows commits as recent as May 2026;
  `apprise-api` (a REST wrapper) is a companion project from the same
  maintainer, itself a useful reference architecture (stateless HTTP
  frontend around the Apprise library — a shape Filearr's own dispatch
  worker can mirror internally rather than shelling out to a separate
  service).
- **Breadth**: ~150+ supported services in one dependency — Discord, Slack,
  Telegram, Gotify, ntfy, PagerDuty, generic webhook ("Custom"/`json://`),
  and SMTP (`mailto://`) all behind one URL-based config string
  (`service://token@host/...`). This is Apprise's core value proposition:
  one config string per channel, one `apprise.notify(body=...)` call
  regardless of destination.
- **Used by prior art already surveyed for Filearr's category**:
  changedetection.io (Apache-2.0) uses Apprise as its own dispatch layer,
  and Recyclarr (an *arr-ecosystem tool, directly adjacent to Filearr's own
  positioning) documents Apprise as a first-class notification backend —
  strong precedent that this is the expected integration point for a
  self-hosted media/file tool, not a stretch dependency.
- **Downsides**: pulls in a **large transitive dependency tree** (one
  package per supported service's SDK/protocol quirks — e.g. optional
  extras for Slack, Twilio, etc.); a single CVE or breaking change in any
  transitive service module is now Filearr's problem to track even for
  channels nobody configured. Apprise's URL-string config format also means
  secrets (API tokens, webhook signing keys) are frequently embedded
  **inline in one opaque string**, which complicates "encrypt this field at
  rest" (§7) — the whole string must be treated as secret, not just a
  sub-field.

### 1.2 ntfy (`binwiederhier/ntfy`)

- ntfy is a **push *transport*, not a rule/dispatch engine** — it explicitly
  does not do alert processing, deduplication, or grouping; it is "any
  system that can make an HTTP request can send a notification" (its own
  positioning). It is a candidate **destination** (Filearr POSTs to a
  self-hosted or public ntfy topic), not a replacement for Filearr's own
  dispatch/throttle/digest logic. Apprise already has an ntfy adapter
  (`ntfy://`), so choosing Apprise as the dispatch layer does not lose ntfy
  as a channel — it's additive, not either/or.

### 1.3 Shoutrrr (`containrrr/shoutrrr`, Go)

- **License: MIT**. Actively maintained (a fork/successor namespace
  `nicholas-fedor/shoutrrr` appears in current Go package search results,
  suggesting the original `containrrr` org's maintenance has partly
  transferred — treat exact current canonical import path as needing a
  spot-check at implementation time, not settled fact here). Stable at
  major version v1; used by Watchtower and kured as their notification
  layer.
- **Relevance to Filearr specifically**: Shoutrrr is a **Go library**, which
  matters because v3's agents are Go (per
  `docs/research/phase-5-distributed-agents.md` §2.6 recommendation — Go
  chosen for single-host cross-compilation, `modernc.org/sqlite` for
  cgo-free FTS5). An **offline agent that wants to raise a local alert
  before reconnecting to the central server** (e.g. "this laptop's watched
  folder had a ransomware-shaped burst of `modified` events") needs a
  Go-native notify path if it dispatches directly rather than only queuing
  the event for the central server to evaluate. Shoutrrr is the natural
  fit for that agent-local case — same category tool as Apprise, but in the
  agent's own language, avoiding a Python subprocess/sidecar inside a Go
  binary.
- Narrower service coverage than Apprise (dozens, not ~150+), but covers the
  practical majority (webhook/generic, SMTP via a service adapter, Slack,
  Discord, ntfy, Mattermost, Telegram, Gotify, PagerDuty-adjacent).

### 1.4 Hand-rolled webhook + SMTP only

- Two well-understood primitives (HTTP POST with HMAC signing; SMTP via
  `aiosmtplib`) cover the two channels the roadmap lists as **required**
  (webhook, email); "Apprise → Discord/ntfy/etc." is explicitly framed in
  the roadmap as reaching *further* services through Apprise, not as the
  baseline. A hand-rolled core:
  - Has a **minimal, auditable dependency surface** (no transitive SDK
    zoo) — directly serves the security-first constraint ordering.
  - Lets Filearr fully own the retry/backoff/idempotency contract (§4)
    instead of inheriting Apprise's own retry semantics (which are
    per-plugin and not guaranteed uniform).
  - Cannot alone reach the "etc." breadth (Discord embeds, Telegram,
    PagerDuty, ntfy priorities) users will expect from a modern
    self-hosted alerting feature, without reimplementing dozens of
    provider-specific payload shapes.

### 1.5 Recommendation: **layered — core webhook + SMTP in-tree, Apprise as an optional adapter**

- **Core (always available, zero extra dependency risk)**: a `webhook`
  channel type (generic HTTP POST, HMAC-SHA256 request signing, configurable
  headers/timeout, SSRF-guarded per §7) and an `email` channel type
  (`aiosmtplib`, §5) ship as first-class, in-tree channel drivers. These
  cover the two channels the roadmap calls out explicitly and need no new
  runtime dependency beyond `aiosmtplib`.
- **Optional adapter**: an `apprise` channel type wraps the `apprise`
  package as an **optional extra** (`filearr[apprise]` / a Docker image
  variant), used when a rule's channel config specifies
  `type: apprise, url: "<apprise-url-string>"`. This gets the ~150-service
  breadth for users who want Discord/Telegram/PagerDuty/ntfy/etc. without
  forcing that dependency tree onto every deployment (some operators run
  fully airgapped and would rather not pull in dozens of transitive SDKs
  they'll never use). Apprise's own delivery result (success/fail per
  service) is normalized into Filearr's channel-abstraction result type
  (§dispatch worker design) so retry/backoff/digest logic stays uniform
  regardless of which channel driver fired.
- **Agent-local (v3 only)**: the Go agent embeds Shoutrrr (or a minimal
  hand-rolled Go webhook/SMTP pair, mirroring the server's own
  core-vs-optional split) strictly for the narrow "local rule matched,
  central server unreachable, deliver now" case (§6 offline-agent design).
  This is deliberately **not** meant to reach Apprise's full breadth on the
  agent — it only needs to hit the same webhook/email/ntfy destinations the
  central server can, as a fallback path.

This layering directly serves the constraint order: **security** (minimal
default dependency surface, no forced transitive SDK tree), **compatibility**
(Apprise remains available for the long tail without being mandatory).

---

## 2. Rule engines / alert-rules prior art

### 2.1 Grafana Alerting

- Model: **alert rule → evaluated condition → routes into a notification
  policy tree → contact point**, with **mute timings** (recurring, e.g.
  "every weeknight") layered separately from **silences** (one-off, e.g. a
  maintenance window) — Grafana explicitly documents these as two different
  primitives for two different use cases, not one mechanism.
- **Timing knobs on notification policies**: `group_wait` (delay before the
  *first* notification for a new group so more alerts can join it),
  `group_interval` (delay before notifying about *changes* to an existing
  firing group), `repeat_interval` (resend cadence for an unresolved,
  unchanged group; default 4h in Grafana). Mute timings do **not** stop
  rule evaluation or hide firing alerts from the UI — they only suppress
  the *notification*, which is the correct separation for Filearr too
  (a suppressed alert-rule match should still be visible/auditable, just
  not spammed to a channel).
- **Directly reusable for Filearr**: the *rule → condition → routing →
  throttle* pipeline shape, and the mute-timing/silence split (Filearr's
  "per-rule throttling/digests" maps onto `group_interval`/`repeat_interval`;
  a future "pause this rule during a bulk re-scan" maps onto a silence).

### 2.2 Prometheus Alertmanager

- **Grouping** (`group_by`): alerts sharing the configured label set are
  batched into **one notification** instead of one-per-alert — the direct
  precedent for Filearr's "digest" requirement (e.g. group by
  `rule_id` + `library_id` so one scan's 400 file-created events under a
  watched glob become one digest message, not 400 webhooks).
- **Inhibition**: mutes a target alert set when a source alert (typically a
  broader/parent condition) is already firing, keyed on shared label
  values. Analogous Filearr use case: if `scan_failure` is firing for a
  library, suppress `extract_error_spike` alerts scoped to the same library
  for the duration (the scan failure is the root cause; the error-spike
  alert is noise on top of it). Worth adopting as a v2-of-alerting
  refinement, not required for MVP.
- **Silences**: matcher-based, time-boxed manual mutes — same concept as
  Grafana's, useful for "I know this rule is noisy right now, quiet it for
  2 hours" without editing the rule itself.
- **Timing model directly reusable**: `group_wait` / `group_interval` /
  `repeat_interval` is the *same three-knob shape* Grafana adopted from
  Alertmanager. Filearr should copy this vocabulary rather than invent new
  terms — operators moving from Grafana/Prometheus backgrounds will
  recognize it immediately.

### 2.3 Healthchecks.io (dead-man's-switch precedent)

- **License: BSD-3-Clause** (Python/Django), fully AGPL-compatible, and a
  clean reference implementation to study directly (source available).
- **Model**: each monitored target has a `period` (expected check-in
  interval) and a `grace` (additional wait before alerting on a late
  check-in) — this is the **exact shape** needed for Filearr's
  "agent-offline" operational alert: `last_seen` heartbeat timestamp +
  `expected_interval` + `grace_period`, alert fires only after
  `last_seen + expected_interval + grace` has elapsed. Recommend lifting
  this period+grace vocabulary verbatim rather than a single flat timeout,
  since it cleanly supports agents with different expected check-in
  cadences (a laptop that syncs hourly vs. a server that syncs every
  minute).

### 2.4 changedetection.io

- **License: Apache-2.0.**
- Uses a **plugin architecture (pluggy) + json-logic** for its condition
  evaluation, letting users compose "if field X compared-to Y" rules
  without a bespoke DSL, and **itself uses Apprise for dispatch** —
  independent confirmation that Apprise is the standard dispatch layer in
  exactly this application category (self-hosted, single-operator,
  file/content-watching tools).
- Less relevant as a rule-*schema* precedent than Grafana/Alertmanager
  (json-logic is more expressive than Filearr's rules need to be — a path
  glob + event-type enum + optional hash-change is a much narrower need
  than "arbitrary boolean logic over scraped page diffs") but confirms the
  "plugin-registrable condition, don't force one hardcoded rule shape"
  instinct is sound for future extensibility.

### 2.5 Watchtower

- Uses Shoutrrr for its own notifications (see §1.3) — no independent rule
  engine of note (Watchtower's "rule" is simply "a container image was
  updated"), included here only to confirm Shoutrrr's real-world production
  usage, not as a rule-model precedent.

### 2.6 Minimal viable rule model for Filearr

Synthesizing the above into the smallest model that covers the roadmap's
stated scope:

```
AlertRule
  id, name, enabled
  scope: library_id (nullable = all libraries) | agent_group_id (v3)
  match:
    path_glob: str            -- pathspec/gitignore-style (§3 below, reuse
                                  phase-2's pathspec decision)
    event_types: [created|modified|deleted|moved]  -- one or more
    hash_change: bool          -- optional: only fire if content_hash changed
                                  (meaningful only for `modified`)
  throttle:
    group_by: [event_type, library_id]  -- Alertmanager-style label set,
                                            fixed vocabulary for v1 (not
                                            arbitrary user fields yet)
    group_wait_s: int          -- default 30s
    digest_window: null | "hourly" | "daily"  -- None = fire per group_wait
                                                 window; else roll up into
                                                 one message per window
    repeat_interval_s: int | null  -- resend cadence if condition keeps
                                       matching (null = fire once per
                                       digest/group cycle only)
  channels: [channel_id, ...]  -- fan-out to N channels per rule
  is_system: bool              -- true for built-in operational rules (§6),
                                   read-only in the rule-builder UI

Channel
  id, name, type: webhook | email | apprise
  config: JSONB (validated per type; secrets flagged, see §7)
  enabled

AlertEvent (durable log — see §3)
  id, rule_id, item_id (nullable, null for operational alerts),
  library_id (nullable), event_type, occurred_at, dedup_key, matched_at,
  delivered: bool, delivery_attempts, last_error

AgentHeartbeat (v3, feeds the agent-offline system rule)
  agent_id, last_seen_at, expected_interval_s, grace_s
```

This mirrors Grafana/Alertmanager's rule→condition→routing→throttle
pipeline and Healthchecks' period+grace model, while deliberately **not**
importing Alertmanager's full label-matcher expressiveness or
changedetection.io's json-logic — v1 fixes `group_by` to a small enum set
(`event_type`, `library_id`, `rule_id`) rather than arbitrary label
matching, which is simpler to implement, review, and secure, consistent
with "smallest thing that satisfies the stated roadmap scope."

---

## 3. Event sourcing for file events: durable log vs evaluate-inline

### 3.1 The tension

`backend/filearr/tasks/scan.py` already computes `new`/`changed`/`missing`/
`moved` counts inline during the walk+diff+tombstone pass (see the
`run.stats` dict built at the end of `_scan_body`), but these are **scan-run
aggregates**, not a per-item durable event stream — there is currently no
row that says "item X transitioned created→modified at timestamp T." Two
designs are on the table:

**(A) Durable `file_events` table.** Every diff-detected transition (new
row → `created`; size/mtime delta → `modified`; vanished → `deleted`;
move-plan applied → `moved`) is written as one row, in the same
batched-commit transaction the scan already uses (every 250 files, per
architecture invariant 5). Alert rule evaluation becomes a separate
consumer (a Procrastinate task, deferred **after** the batch commit,
identical timing discipline to `_defer_extract_batch`) that reads
unconsumed `file_events` rows and matches them against active
`AlertRule`s.

**(B) Evaluate inline during scan, no event table.** Rule matching happens
directly inside `_scan_body`'s existing per-file loop (or immediately after,
against the in-memory `pending_extract`/new-item lists), and only a
**match** (rule fired) is persisted — as an `AlertEvent`/dispatch-queue row,
never a row for every single file transition regardless of whether any
rule cared about it.

### 3.2 Postgres bloat at 100k-file scans — the deciding factor

The project's own CLAUDE.md records a real production data point: "First
real scan: 1,190 files / 50 s." A 100k-file library is not hypothetical for
this project's target deployments (Unraid/large media libraries). Under
design (A), a `created`-heavy first scan of such a library writes **100k+
event rows in one scan run**, every single run, whether or not a single
alert rule is configured to care about any of them. This is the same class
of problem the Postgres research above flags directly: unbounded
high-write-volume event tables need partitioning + a drop-not-delete
retention policy from day one, or they bloat and compete with the
scan/extract write path this project already treats as latency-sensitive
(batched commits exist specifically to avoid long-held transactions and
publish progress promptly).

Design (B) sidesteps this entirely for the common case (a fresh 100k-file
first scan with **zero** matching alert rules writes **zero** new rows
beyond the ordinary `items` upserts already happening) but has a real cost:
**it couples rule evaluation to the scan's hot path**, meaning a
misbehaving or slow rule-matching pass (e.g. a pathological glob) now sits
inside the same batched-commit loop invariant 5 protects, or requires
threading match candidates through an additional in-memory list alongside
`pending_extract`/`new_item_ids` (already a growing set of per-scan
in-memory lists — see the existing `pending_extract`, `new_item_ids`
tracked in `scan.py`).

### 3.3 Recommendation: **hybrid — evaluate inline against in-memory transition data, persist only on match, with a small bounded "recent transitions" ring for digest windows**

- **Do not create a durable `file_events` table that logs every scan
  transition unconditionally.** This is the single highest-leverage
  decision in this brief for avoiding operational regret at scale: it
  directly contradicts nothing in the roadmap (the roadmap says "runs on
  agent events post-replication" — it does not mandate a full durable log
  of every transition server-side) and avoids importing the exact
  Postgres-bloat failure mode the retention-strategy research warns about.
- **Rule matching happens as a lightweight in-memory pass over the same
  transition data the scan loop already computes** (`new`/`changed`/
  `missing`/`moved` categorization, plus the item's `rel_path` and, for
  `modified`, whether `content_hash`/`quick_hash` changed — already
  computed when the hash policy allows it, per T7). This is a pure
  function over data the scan already has in hand; it does **not** need a
  new DB write per file, only a cheap `pathspec` match (§3.4) against each
  transitioning item against the set of currently-enabled `AlertRule`s
  (loaded once per scan run, not per file).
- **Persist only `AlertEvent` rows for actual rule matches** — this table
  is small in practice (bounded by how many rules exist × how often they
  actually match, not by total files scanned) and is the correct place to
  put durable delivery-tracking state (`delivered`, `delivery_attempts`,
  `dedup_key`) since that state genuinely needs to survive process
  restarts and support retry/backoff (§4).
- **Digest windows do not need a separate durable ring buffer.** A digest
  is "roll up N matches into one message" — this is naturally expressed as
  `AlertEvent` rows accumulating with `delivered = false` until the digest
  scheduler (§4, a Procrastinate periodic task, T5-tick pattern) flushes
  them in one batch and marks them delivered. No additional table needed;
  `AlertEvent` already serves as both the match record and the
  pending-delivery queue.
- **Retention**: `AlertEvent` rows, once `delivered = true`, are candidates
  for a scheduled purge (mirroring the existing tombstone/recycle-bin
  retention pattern already established for `items` — architecture
  invariant 4 — so this is a *consistent* addition to the project's
  existing retention-policy vocabulary, not a new concept). A default
  30-day retention with a periodic purge task is recommended; this table
  never approaches `items`-table scale because it is match-gated, not
  transition-gated.

### 3.4 Glob engine: reuse the phase-2 `pathspec` decision

`docs/research/phase-2-indexing-controls.md` §"Recommendation" already
settled this for include/exclude globs: **`pathspec >= 1.1.0` (MPL-2.0)**,
using `pathspec.GitIgnoreSpec` for correct gitignore-fidelity negation
semantics, chosen over `wcmatch` specifically because pathspec is the only
surveyed library that gets gitignore precedence and negation right natively.
Alert-rule `path_glob` matching should call the **same** `pathspec.GitIgnoreSpec`
oracle — not a second glob engine, not `fnmatch` (which `scan.py`'s `walk()`
uses today for include/exclude, per phase-2's own note that fnmatch and
gitignore syntax "mostly overlap for simple patterns" but are not
identical). Using one matching engine for both scan-time
include/exclude *and* alert-rule path matching means a user who understands
one glob dialect in the product understands both, and avoids a second
dependency for the same job. This is a direct, low-risk reuse — no new
research needed, just consistent application of an already-settled
decision.

### 3.5 Interplay with the v3 agent outbox — same event shape?

`docs/research/phase-5-distributed-agents.md` §4 defines the agent-side
`outbox` table (`seq_no`, `item_id`, `op: upsert|tombstone`, `payload`) as
the **replication** mechanism — its purpose is getting an agent's local
SQLite changes into central Postgres idempotently, keyed on
`(agent_id, seq_no)`. This is a different concern from alert-event matching
(replication answers "what changed and needs syncing"; alerting answers
"did a change match a rule the user cares about"), but they should **share
a source vocabulary**: the outbox's `op` (`upsert`/`tombstone`) and
Filearr's alert `event_types` (`created`/`modified`/`deleted`/`moved`) are
not currently the same enum, and reconciling them is worth doing
deliberately rather than accidentally:

- **Recommendation: do not unify the tables, but unify the transition
  vocabulary.** An agent's local SQLite scanner should compute the same
  four-way `created`/`modified`/`deleted`/`moved` classification server-side
  scans already produce (an `upsert` in outbox terms is ambiguous between
  "new" and "changed" from the replication protocol's point of view, since
  outbox's job is idempotent upsert semantics, not event semantics — but
  the agent's *local* scan diff, which is what feeds local rule evaluation
  per the "local evaluation on offline agents" requirement, already knows
  which of the two it was). Concretely: the agent's local scan diff
  produces the same four-way classification Filearr's server-side
  `scan.py` produces (reusing the ported scan logic per phase-5 §2.6's
  reuse strategy), evaluates local `AlertRule`s (synced down via the same
  config-push channel used for indexing policy) against that classification
  **before** translating it into an outbox `upsert`/`tombstone` row for
  replication. This keeps the two concerns cleanly layered: outbox =
  replication protocol, local diff classification = alerting input, and
  they share a code path (the ported diff logic) without sharing a
  database table.
- Central-side alerts for **agent-sourced** items (v3) consume the
  replicated item rows the same way local scans do today — no special-casing
  needed once replication lands, since a replicated upsert/tombstone is
  processed by the same central scan-adjacent diff/classify logic that
  already runs.

---

## 4. Throttling / digest design

### 4.1 Model: adopt Alertmanager's `group_wait` / `group_interval` /
`repeat_interval` vocabulary directly (§2.2), applied per-`AlertRule`:

- **`group_wait_s`** (default 30s): after the *first* match for a
  previously-quiet rule, wait this long before sending, so near-simultaneous
  matches (e.g. a batch move of 40 files under one glob) collapse into one
  notification instead of 40.
- **`digest_window`** (`null` | `hourly` | `daily`): when set, matches are
  **not** dispatched at `group_wait` cadence at all — they accumulate as
  undelivered `AlertEvent` rows and are flushed in bulk by a scheduled
  digest task at the window boundary (see §4.3). This directly answers the
  roadmap's explicit "digests" requirement as a distinct mode from ordinary
  throttled-but-immediate delivery.
- **`repeat_interval_s`** (nullable): if the same `dedup_key` (see §4.2)
  keeps matching after an initial notification, resend only after this
  interval has elapsed — prevents an alert on a still-failing/still-matching
  condition from going silent forever after the first send, while not
  spamming on every single subsequent match either. Mirrors Alertmanager's
  behavior for an alert group that stays firing without resolving.

### 4.2 Dedup keys

Each `AlertEvent` computes a `dedup_key` = hash of
`(rule_id, group_by-selected fields)` — for the fixed v1 `group_by` vocabulary
(`event_type`, `library_id`, `rule_id`; §2.6), this is deliberately coarse
(e.g. "all `modified` events in library X matching rule Y this window" is
one key, not one key per file) so the throttle/digest windowing operates
per-group, not per-item — this is precisely Alertmanager's `group_by`
semantics, reused rather than reinvented.

### 4.3 Store-and-flush via a Procrastinate periodic task (T5 pattern)

The T5 scheduling decision already establishes the pattern to reuse: **a
static Procrastinate `@periodic` task on a 1-minute tick**
(`filearr.worker.schedule_scans` is the existing precedent — cronsim
evaluation of a cron expression against the tick, `queueing_lock` collapsing
duplicate ticks). A new `filearr.worker.flush_alert_digests` periodic task,
same 1-minute tick, does:

1. Query `AlertEvent WHERE delivered = false AND rule.digest_window IS NOT
   NULL` grouped by `(rule_id, dedup_key)`.
2. For each group, check whether the digest window boundary has been
   crossed (hourly: top of the hour since the group's earliest event;
   daily: local-configured daily time) — this is the same "evaluate a cron-
   like expression against the tick" shape T5 already solved, just with a
   fixed hourly/daily cadence instead of an arbitrary user cron string, so
   it is simpler than `schedule_scans`, not harder.
3. Flush due groups: render one digest notification listing the N matched
   items (path, event type, timestamp — HTML/text-escaped per §5.3), send
   via the rule's configured channels, mark all rows in the group
   `delivered = true`.
4. Non-digest rules (`digest_window IS NULL`) are **not** handled by this
   periodic task at all — they are dispatched by the immediate dispatch
   worker (§dispatch design below) respecting only `group_wait_s`, which is
   short enough (seconds) to handle inline rather than waiting for a
   minute-granularity tick.

This reuses the T5 periodic-tick precedent exactly (no new scheduling
primitive invented) and needs no Redis (Postgres-backed Procrastinate
queries + a `queueing_lock` per rule to prevent double-flush on a
duplicate/late tick, same collapsing mechanism `schedule_scans` uses for
scans).

---

## 5. Email: SMTP config, aiosmtplib, template safety

### 5.1 SMTP config handling precedent

- **Paperless-ngx**: flat env-var-driven SMTP config (`PAPERLESS_EMAIL_HOST`,
  `_PORT`, `_HOST_USER`/`_HOST_PASSWORD`, TLS/SSL toggles), defaults to
  `localhost:25` with auth/TLS off — i.e., SMTP is opt-in, degrades to "no
  email" cleanly when unconfigured, matching Filearr's own "everything
  degrades gracefully, nothing is mandatory" posture (per invariant-style
  thinking already used elsewhere in this project, e.g. hash policy
  degrading under `quick_only`).
  Recommendation: mirror this — `FILEARR_SMTP_HOST`/`_PORT`/`_USER`/
  `_PASSWORD`/`_STARTTLS`/`_FROM` env vars for a **default/fallback SMTP
  channel**, plus **per-channel SMTP override** stored in a `Channel.config`
  JSONB row for users who want per-rule sender identities (e.g. "alerts
  from this library go out as alerts@mydomain", a real feature Grafana/
  Immich-style multi-tenant self-hosted tools commonly expose). Immich's
  own SMTP settings (searched but not independently re-verified in this
  pass — flagged) follow the same "single admin-configured SMTP relay,
  per-notification-type toggles on top" shape common across this app
  category; treat this as reinforcing, not load-bearing, evidence.
- Filearr should **not** attempt to be its own MTA (no embedded SMTP
  server) — always relay through a configured external SMTP host/relay,
  consistent with every surveyed precedent.

### 5.2 aiosmtplib

- **Confirmed MIT-licensed, Production/Stable maturity classifier, actively
  released (2026-06-20 release found in search)**, requires Python ≥3.10
  (comfortably under Filearr's pinned 3.13). Provides both a simple
  `aiosmtplib.send()` one-shot call and a lower-level `SMTP` client class
  for connection reuse — the dispatch worker (§ below) should hold a
  reused connection pool per configured SMTP relay rather than reconnecting
  per email, both for latency and to avoid tripping relay rate limits
  during a digest flush that might send many emails in one batch.
- Supports STARTTLS/implicit-TLS/plain, matching the toggle set Paperless-ngx
  exposes; recommend defaulting to STARTTLS-required (reject downgrade to
  plaintext unless explicitly opted out per channel) — consistent with the
  security-first constraint ordering.

### 5.3 Template rendering: jinja2, already transitively available, injection-safe

- FastAPI's own dependency chain does not mandate jinja2, but it is an
  extremely common transitive presence in this stack's category (starlette
  optionally uses it for `Jinja2Templates`) — **verify at implementation
  time whether it is already present via FastAPI/starlette's optional extra
  or needs adding explicitly**; either way it is a small, mature,
  BSD-3-Clause-licensed dependency with no license friction.
- **Critical safety point, specific to Filearr's data**: alert notification
  bodies interpolate **file paths and filenames**, which per
  `backend/filearr/errors.py`'s own documented threat model are
  **UNTRUSTED, attacker-influenceable strings** (a crafted filename can
  embed control characters or, for HTML-rendered emails, markup). The
  existing `sanitize_error()` pattern (strip control chars, cap length) is
  the right **first** layer and should be applied to path/filename values
  before they ever reach a template context, exactly as it already is for
  `_extract_error` strings. On top of that:
  - **Never put user/file-derived strings into the template *source*** —
    only ever pass them as **variables** into a **pre-defined, admin/
    developer-authored template file** (digest email template, webhook
    payload template). This closes the actual SSTI (server-side template
    injection) class outright, per the OWASP-aligned guidance found: "the
    defense is not to sandbox Jinja2; it is to keep user input out of the
    template position." Filearr has no legitimate feature requirement for
    *user-authored* templates in v1/v2 (no roadmap item asks for
    "customize your own alert email HTML"), so this constraint costs
    nothing to satisfy.
  - **Use `Environment(autoescape=select_autoescape(["html", "xml"]))`**
    for HTML email bodies specifically — this is the standard Jinja2 XSS
    defense (auto-escape any interpolated filename/path containing `<`,
    `&`, etc. before it lands in an HTML `<td>`/`<li>`) and is a one-line
    configuration, not optional. Plain-text digest bodies (and webhook JSON
    payloads, which should be built via `json.dumps` of a data structure,
    **never** via string templating at all) don't need HTML escaping but
    do need the control-character stripping `sanitize_error` already
    provides.
  - If a **sandboxed environment** is ever wanted as defense-in-depth (e.g.
    if a future feature does let admins customize digest templates),
    `jinja2.sandbox.SandboxedEnvironment` is the documented mechanism
    (restricts attribute access, blocks dangerous builtins) — flagged here
    as a **future** hardening layer, not required for the "developer-authored
    templates only" v1/v2 scope.

---

## 6. Operational alerts (dogfooding the same rule model)

All four operational alert types below are implemented as **`is_system =
true` `AlertRule` rows**, seeded at migration/bootstrap time, editable only
for their throttle/channel settings (not their match logic) in the rule-
builder UI (§UI notes) — this is a deliberate dogfooding choice: the same
`AlertEvent`/dispatch/digest pipeline built for user file-watch rules
handles operational alerting too, so there is exactly one delivery pipeline
to secure, test, and operate, not two.

### 6.1 Scan failures

- Trivial to wire: `scan_library`'s existing crash handler in `scan.py`
  already does `run.status = "failed"` + `run.stats = {..., "error":
  sanitize_error(exc)}` (architecture invariant 7 — a crashed scan must
  never stay `running`). The system rule's match condition is simply
  "a `ScanRun` transitioned to `status = failed`" — evaluated at the exact
  point the existing exception handler commits, not a new poll. No new
  detection logic needed, only a dispatch hook off an event that already
  exists in code today.

### 6.2 Extract-error spikes: threshold, not anomaly detection

- Per the roadmap's own framing ("threshold vs anomaly — keep threshold")
  and this project's constraint ordering (simplicity/auditability over
  cleverness), the recommended rule is a **simple rate-over-window
  threshold** against the *already-existing* authoritative aggregate:
  `backend/filearr/errors.py`'s `extract_error_count()` /
  `extract_error_counts_by_library()` (GIN-indexed, cheap, already the
  documented authoritative source per T11's design note distinguishing it
  from the best-effort per-run counter). Concretely: "if
  `extract_error_counts_by_library()[library_id]` increases by more than N
  in a rolling W-minute window, fire" — computed by a periodic task
  (reusing the same 1-minute tick infrastructure as §4.3) that snapshots
  the count and diffs against the prior snapshot, **not** a new anomaly
  model. This is intentionally the simplest thing that satisfies "extends
  Phase-1 T11" without inventing statistical machinery the roadmap
  explicitly said to avoid.
- Threshold (`N` per `W` minutes) is a per-system-rule config field, same
  shape as a user rule's throttle config — reusing the schema, not adding a
  parallel one.

### 6.3 Agent-offline (v3): Healthchecks-style period+grace

- Directly reuses §2.3's `period`/`grace` model: each enrolled agent (v3)
  has an expected check-in interval (derived from its config-push/outbox
  cadence) and a grace period; the periodic tick task compares
  `now - agent.last_seen_at` against `expected_interval + grace` and fires
  the system rule if exceeded. This is the same "last event timestamp +
  tolerance window, checked on a tick" shape as scan-failure detection and
  extract-error-spike detection — all three operational alerts reduce to
  "periodically compare a stored timestamp/counter against a threshold,"
  which is precisely why dogfooding one rule engine for all of them is
  sound engineering, not just a scope-reduction shortcut.

### 6.4 Replication lag (v3): seq_no delta

- Per `docs/research/phase-5-distributed-agents.md` §4, the server already
  tracks `agents.last_contiguous_seq_no`. Replication lag is simply
  "agent's locally-known max `seq_no` (reported in each replication batch's
  metadata, per phase-5's own note about a cheap extra field on the outbox
  POST) minus `last_contiguous_seq_no`, if this delta exceeds a threshold
  for longer than W minutes, fire." Same threshold-over-window shape as
  §6.2, reusing the identical periodic-comparison pattern.

---

## 7. Security notes

### 7.1 Webhook SSRF

Webhook channel configs let an admin-level user supply an arbitrary URL
that Filearr's own server will then make an outbound HTTP request to —
textbook SSRF surface, and explicitly called out in the task brief.
Recommended controls, synthesized from the OWASP SSRF cheat sheet and
current (2025-2026) webhook-security writeups surveyed:

1. **Resolve-then-validate, not validate-then-resolve.** Parse the
   webhook URL's hostname, resolve it via DNS **once**, and validate the
   **resolved IP** (not just the literal hostname string) against a
   deny-list before connecting — string-level hostname checks alone are
   defeated by DNS rebinding (a hostname that resolves to a public IP at
   validation time and a private IP at connection time). Practically: use
   an HTTP client configured with a custom `Connection`/resolver hook that
   re-validates the IP **at actual socket-connect time**, not only at a
   pre-flight check, since a naive "resolve once at validation, then let
   the HTTP library re-resolve on connect" still has a TOCTOU gap.
2. **Deny-list private/reserved CIDR ranges by default**: `127.0.0.0/8`,
   `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, link-local
   `169.254.0.0/16` (notably where cloud metadata endpoints like
   `169.254.169.254` live — a classic SSRF-to-credential-theft target),
   and IPv6 equivalents (`::1/128`, `fc00::/7`, `fe80::/10`). This should
   be a **default-deny** posture per the security-first constraint
   ordering, not default-allow-with-opt-out.
3. **Explicit operator override, not silent allowlist creep.** The
   research surfaced a real tension: self-hosted deployments often *want*
   to hit an in-cluster/in-LAN service (a local ntfy instance, a Home
   Assistant webhook on the same LAN as the Filearr host) — exactly the
   private-IP ranges the default deny-list blocks (this is the same
   friction reported in real issues against Zitadel's outgoing-webhook
   denylist blocking legitimate RFC1918 targets). Recommendation: ship
   default-deny, but expose a per-installation
   `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS` env var (explicit, admin-only,
   documented as "you are widening SSRF blast radius, only enable for
   trusted-LAN targets") rather than a per-rule bypass toggle a
   lower-privileged user could set — the override must require the same
   trust level as server configuration, not rule-authoring.
4. **Timeouts + response-size caps** on every webhook dispatch (a
   malicious or misconfigured endpoint should not be able to hang a
   dispatch worker or exhaust memory reading an unbounded response body) —
   standard webhook-worker hygiene, not SSRF-specific, but belongs in the
   same hardening pass.
5. **HMAC-sign outbound webhook payloads** (a shared secret per channel,
   `X-Filearr-Signature: sha256=...` header) so the *receiving* endpoint can
   verify authenticity — this protects the receiver, not Filearr itself,
   but is expected practice for any webhook-sending product and costs
   little to add now while building the channel abstraction.

### 7.2 Secrets in channel configs — encrypted at rest?

- pgcrypto (Postgres-side) was evaluated and is **not recommended**: its
  encrypt/decrypt happens server-side, meaning the key must live somewhere
  the database process (or an admin with DB access) can reach it, which
  does not meaningfully protect against the threat model that matters here
  (a stolen Postgres backup/dump, or DB-level access separate from
  application-level access).
- **Recommendation: application-level envelope encryption.** Encrypt
  `Channel.config` secret sub-fields (SMTP password, webhook HMAC secret,
  Apprise URL strings that embed tokens) using a key held **outside**
  Postgres — e.g. `FILEARR_SECRET_KEY` (an env var / mounted secret file,
  consistent with how `FILEARR_*` config already works per CLAUDE.md) used
  to derive a symmetric key (AES-GCM via Python's `cryptography` library,
  MIT/Apache-2.0 dual-licensed, no AGPL friction) that encrypts secret
  fields before they are written to the `Channel.config` JSONB column, and
  decrypts only in-process at dispatch time. This means a stolen Postgres
  dump alone does not expose channel credentials — the application secret
  key is a separate, independently-managed piece, matching the "keys
  outside the database, plaintext never on the server [DB server]"
  property application-level encryption offers over pgcrypto.
  - Because Apprise URL strings frequently embed the entire secret inline
    (§1.1's noted downside), the encryption boundary for an `apprise`-type
    channel must be **the whole config string**, not a sub-field — document
    this explicitly so a future maintainer doesn't assume field-level
    encryption grain uniformly applies.
- **Never log decrypted channel configs.** The dispatch worker's error/
  retry logging (§ dispatch design) must redact `config` entirely in log
  output, not merely the fields it happens to know are secret (an Apprise
  URL's *entire* value is sensitive, per the point above) — log only
  `channel_id`/`channel_type`/`rule_id` plus the sanitized delivery error.

### 7.3 Template injection

Covered in depth in §5.3 — the load-bearing control is "never place
untrusted (file-path/filename) strings in the template *source* position,
only ever as *variables* into a pre-authored template," combined with
HTML-autoescaping for HTML bodies and the existing `sanitize_error()`
control-character stripping applied to any path/filename before it reaches
a template context. No sandboxed-environment requirement for v1/v2 since no
roadmap item asks for user-authored templates.

---

## 8. Recommended architecture summary

### 8.1 Rule schema DDL (illustrative — see §2.6 for full field list)

```sql
CREATE TABLE alert_channels (
    id          uuid PRIMARY KEY DEFAULT uuidv7(),
    name        text NOT NULL,
    type        text NOT NULL CHECK (type IN ('webhook','email','apprise')),
    config      jsonb NOT NULL,   -- secret sub-fields encrypted (§7.2)
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_rules (
    id                uuid PRIMARY KEY DEFAULT uuidv7(),
    name              text NOT NULL,
    enabled           boolean NOT NULL DEFAULT true,
    is_system         boolean NOT NULL DEFAULT false,  -- dogfooded ops rules (§6)
    library_id        uuid REFERENCES libraries(id) ON DELETE CASCADE,  -- null = all
    path_glob         text,                 -- pathspec.GitIgnoreSpec syntax (§3.4)
    event_types       text[] NOT NULL,      -- created|modified|deleted|moved
    hash_change_only  boolean NOT NULL DEFAULT false,
    group_by          text[] NOT NULL DEFAULT '{event_type,library_id,rule_id}',
    group_wait_s      integer NOT NULL DEFAULT 30,
    digest_window     text CHECK (digest_window IN ('hourly','daily') OR digest_window IS NULL),
    repeat_interval_s integer,
    -- system-rule-only threshold config (§6.2/6.4), null for user rules:
    threshold_count   integer,
    threshold_window_s integer,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_rule_channels (   -- fan-out, many-to-many
    rule_id     uuid NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    channel_id  uuid NOT NULL REFERENCES alert_channels(id) ON DELETE CASCADE,
    PRIMARY KEY (rule_id, channel_id)
);

CREATE TABLE alert_events (          -- match record + delivery queue (§3.3)
    id                uuid PRIMARY KEY DEFAULT uuidv7(),
    rule_id           uuid NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    item_id           uuid REFERENCES items(id) ON DELETE SET NULL,  -- null for ops alerts
    library_id        uuid REFERENCES libraries(id) ON DELETE SET NULL,
    event_type        text NOT NULL,
    dedup_key         text NOT NULL,        -- hash(rule_id, group_by field values)
    occurred_at       timestamptz NOT NULL DEFAULT now(),
    delivered         boolean NOT NULL DEFAULT false,
    delivered_at      timestamptz,
    delivery_attempts integer NOT NULL DEFAULT 0,
    last_error        text                  -- sanitize_error()'d, capped (errors.py precedent)
);
CREATE INDEX ix_alert_events_pending ON alert_events (rule_id, dedup_key) WHERE NOT delivered;
-- Retention: periodic purge of delivered rows older than N days (mirrors
-- items tombstone/recycle-bin retention, invariant 4).
```

### 8.2 Event capture point in the scan pipeline

Inline in `_scan_body` (`backend/filearr/tasks/scan.py`), immediately after
each item's `new`/`changed`/`missing`/`moved` classification is decided
(the existing `if item is None: ... elif item.size != size or ...: ...
else: ...` block, plus the move-detection block after the walk loop) — call
a new `match_alert_rules(item, event_type, hash_changed)` pure function
against the **already-loaded** set of enabled `AlertRule`s for this library
(loaded once at scan start, not per file, same pattern as `enabled_types`
being loaded once). Matches are appended to an in-memory list, alongside
the existing `pending_extract`/`new_item_ids` lists, and flushed to
`alert_events` **in the same batched commit** the scan already performs
every 250 files (`publish_progress()`) — no new transaction discipline, no
violation of invariant 5 (defer-after-commit only applies to job-queue
defers; writing `alert_events` rows is a plain committed insert, not a job
defer, so it has no race to guard against).

### 8.3 Evaluation pipeline: inline (not deferred) for matching; deferred for dispatch

- **Matching** is inline (§3.3, §8.2) — cheap, in-memory, no new DB
  round-trip per file beyond the batched insert.
- **Dispatch** is deferred (Procrastinate task,
  `filearr.tasks.alerts.dispatch_alert_event`), enqueued after the batch
  commit exactly like `_defer_extract_batch` — one job per `alert_events`
  row for non-digest rules (respecting `group_wait_s` via
  `configure_task(..., schedule_in=group_wait_s)` or an equivalent delay),
  or left unqueued (picked up by the digest flush periodic task instead)
  for digest-mode rules.

### 8.4 Dispatch worker design: retry/backoff, channel abstraction

- **Channel abstraction**: a `ChannelDriver` protocol with one method,
  `async def send(self, config: dict, rendered: RenderedAlert) -> DeliveryResult`,
  implemented by `WebhookChannelDriver`, `EmailChannelDriver`, and
  `AppriseChannelDriver` (§1.5's layering). `RenderedAlert` is built once
  (subject/body/payload, templated per §5.3's safety rules) and handed to
  whichever drivers the rule's `alert_rule_channels` fan-out specifies.
- **Retry/backoff**: reuse Procrastinate's own native retry strategies
  (already used elsewhere in this codebase's task infrastructure) —
  exponential backoff (e.g. base 3s: 3s/9s/27s/81s/243s) on the dispatch
  task, retrying only on transient failures (connection refused, timeout,
  5xx from a webhook endpoint) and **not** retrying on definitive
  rejections (4xx from a webhook, SMTP 5xx permanent failure) — mirrors
  Procrastinate's own documented "define which exception types to retry
  on" capability, so this needs no new retry primitive, just correct
  exception classification in `ChannelDriver.send`.
- **Idempotency**: `alert_events.delivered`/`delivery_attempts` is the
  dedup guard — a retried dispatch job checks `delivered` before sending
  (in case a prior attempt actually succeeded but the job crashed before
  marking it), same "check before acting" discipline the scan crash
  handler already models (invariant 7).
- **Failure surfacing**: a failed dispatch (all retries exhausted) writes
  `last_error` (sanitized, capped) onto the `alert_events` row and —
  critically — does **not** silently vanish; it should itself be visible
  via `/api/system` (extending the existing `failed_jobs`/error-surfacing
  endpoints in `api/system.py`) so "my alert channel has been broken for
  three days" is discoverable the same way a failed scan or extract error
  is today (T11 precedent, directly extended).

### 8.5 Digest scheduler

Covered fully in §4.3 — a `filearr.worker.flush_alert_digests` periodic
task on the existing T5 1-minute-tick infrastructure, reusing the
`queueing_lock`-style duplicate-tick collapsing already implemented for
`schedule_scans`.

### 8.6 Offline-agent local evaluation + reconnect delivery (v3)

- The Go agent (per phase-5's architecture) evaluates its **local** copy of
  synced-down `AlertRule`s against its own local scan-diff classification
  (§3.5) **before** replication — this is what "local evaluation for
  offline machines" in the roadmap means concretely: the agent does not
  need central connectivity to know a watched path changed.
- **Delivery while offline**: for rules whose channel config is reachable
  from the agent's own network context (e.g. a webhook on the same LAN, or
  direct SMTP if the agent has outbound internet), the agent can dispatch
  **directly** using its embedded Shoutrrr-or-equivalent driver (§1.3) —
  this serves genuinely latency-sensitive local alerts (e.g. "ransomware-
  shaped burst of modifies on this laptop") without waiting for
  reconnection.
- **Delivery on reconnect**: for rules whose intended channel is only
  reachable from the central server (e.g. a webhook on the server's LAN
  that the roaming laptop can't reach, or a channel the admin wants
  centrally rate-limited/audited), the agent instead queues the **matched
  alert event** (not the raw file event — it already evaluated the rule
  locally) as a small durable row in its own SQLite outbox-adjacent table,
  replicated on reconnect via the same `(agent_id, seq_no)` idempotent
  path as ordinary item replication (§3.5) — the central server, on
  receiving it, writes it straight to `alert_events` as already-matched
  (skipping re-evaluation, since the agent already decided) and dispatches
  normally.
- **Per-rule policy**: whether a rule dispatches agent-local-directly vs.
  central-only should be an explicit rule field (`dispatch_locality:
  agent_local | central | both`), not an implicit inference — this keeps
  the "which alerts can fire without phoning home" decision auditable and
  admin-controlled, matching the RBAC-style explicit-grant posture already
  used elsewhere in the v3 design (`manage_alerts` is already listed as a
  grantable RBAC action in `future-roadmap.md` §3).

---

## 9. UI notes (rule builder — minimal)

- **Rule list**: name, scope (library or "all"), event types (badges),
  enabled toggle, last-matched timestamp, is_system rules visually
  distinguished (e.g. a lock icon) and their match-logic fields read-only
  (only throttle/channel editable, per §6's dogfooding note).
  path/event/throttle fields grouped exactly as the DDL above — one glob
  input (with the same syntax hint text used for library include/exclude
  globs, since it's the same `pathspec` engine per §3.4 — reuse the
  existing glob-help copy, don't write new documentation for a second
  dialect that doesn't exist), a multi-select for event types, a hash-change
  checkbox (disabled/greyed when `event_types` doesn't include `modified`,
  since it's only meaningful there), and a throttle section offering
  "immediate (with N-second grouping)" vs "digest: hourly/daily" as a
  simple radio, not exposing every Alertmanager knob (`repeat_interval` can
  default sensibly and be an "advanced" collapsed field).
- **Channel management**: a separate list (name, type, enabled) — secret
  fields in the config form always render as password-masked inputs that
  never round-trip the actual decrypted value back to the browser on edit
  (send `"unchanged"` sentinel from the backend, only overwrite on
  explicit re-entry) — a standard "don't leak the secret back to the
  client on GET" pattern, worth stating explicitly since it's easy to get
  wrong when building a form that both creates and edits.
- **Test-send button** per channel (send a synthetic test alert) — cheap to
  build on top of the same `ChannelDriver.send` abstraction and
  high-value for operator confidence before wiring a real rule to a new
  channel.
- **Recent alert-events view**: a simple table (rule, item/path, event
  type, delivered/pending/failed status, timestamp) scoped per library —
  the natural extension of the existing Admin page's per-library
  scan-trigger/error-surfacing UI (`frontend/src/lib/AdminPage.svelte`),
  not a new top-level UI section.

---

## 10. Task breakdown (T-numbered, sized, acceptance criteria)

**T-A1 (S) — Alert schema migration.** Add `alert_channels`,
`alert_rules`, `alert_rule_channels`, `alert_events` tables (§8.1) via
Alembic (existing baseline pattern, T9). *Accept*: migration applies
cleanly on top of current baseline; all FKs/indexes present; `init_db.py`
idempotency preserved.

**T-A2 (M) — Channel abstraction + core drivers.** `ChannelDriver`
protocol; `WebhookChannelDriver` (HMAC signing, SSRF guard per §7.1,
timeout + response-size cap); `EmailChannelDriver` (`aiosmtplib`, STARTTLS
default, connection reuse). *Accept*: unit tests cover SSRF deny-list
(private IPv4/IPv6 + DNS-rebinding via a mocked resolver), HMAC signature
verification round-trip, SMTP send via a test relay (e.g. `aiosmtpd` in
tests).

**T-A3 (S) — Apprise adapter (optional extra).** `AppriseChannelDriver`
behind `filearr[apprise]` extra; normalizes Apprise's per-service result
into `DeliveryResult`. *Accept*: works with `apprise` installed; channel
type gracefully errors with an actionable message ("install
filearr[apprise]") when the extra is absent and an `apprise`-type channel
is configured.

**T-A4 (M) — Secret encryption for channel configs.** Application-level
AES-GCM envelope encryption keyed by `FILEARR_SECRET_KEY` for
`alert_channels.config` secret fields (§7.2); mask-on-read API behavior
(never return decrypted secrets to the client; "unchanged" sentinel on
edit). *Accept*: DB dump/backup contains no plaintext secrets;
round-trip encrypt/decrypt test; API response schema never includes
plaintext.

**T-A5 (M) — Inline rule matching in scan pipeline.** `match_alert_rules()`
pure function; wired into `_scan_body`'s per-item classification and the
move-detection block (§8.2); `alert_events` rows written in the existing
batched commit. *Accept*: a scan of a library with an active rule produces
exactly the expected `alert_events` rows for created/modified/deleted/moved
transitions (integration test against a temp directory tree, mutate,
rescan, assert rows); a scan with **no** active rules writes **zero**
`alert_events` rows (confirms the "no unconditional event log" design
holds, §3.3).

**T-A6 (M) — Dispatch worker + retry/backoff.** Procrastinate task
`dispatch_alert_event`, deferred after batch commit (mirrors
`_defer_extract_batch`); exponential backoff on transient failures only;
idempotency check against `alert_events.delivered` before sending.
*Accept*: simulated transient failure (mock 500) retries per backoff
schedule and eventually succeeds on a subsequent mock 200; simulated
permanent failure (mock 400) does not retry; a job that crashes
post-send-pre-mark-delivered does not double-send on retry (idempotency
test).

**T-A7 (S) — Group-wait throttling for immediate (non-digest) rules.**
`group_wait_s` delay via Procrastinate's `schedule_in`/deferred-time
mechanism; dedup-key grouping collapses near-simultaneous matches into one
notification. *Accept*: 40 file-created events under one glob within the
group-wait window produce exactly 1 delivered notification listing all 40
(or a documented cap + "and N more").

**T-A8 (M) — Digest scheduler.** `flush_alert_digests` periodic task on
the T5 1-minute tick; hourly/daily window boundary detection;
`queueing_lock`-style double-flush guard. *Accept*: rule configured with
`digest_window: hourly` accumulates matches for <1h with zero deliveries,
then exactly one digest delivery at the hour boundary containing all
accumulated matches; a duplicate/late tick does not double-deliver.

**T-A9 (S) — Operational alert: scan failures.** Seed `is_system` rule;
wire scan crash handler (`scan.py`) to emit a matching `alert_events` row.
*Accept*: a forced scan failure (e.g. a root that vanishes mid-scan)
produces exactly one alert dispatch to the configured operational channel.

**T-A10 (M) — Operational alert: extract-error-spike threshold.** Periodic
snapshot/diff of `extract_error_counts_by_library()` against a rolling
window (§6.2). *Accept*: seeding N corrupt files above threshold within
the window fires; below-threshold does not; window rolls off correctly
(old errors age out of the comparison).

**T-A11 (L, v3-gated) — Operational alerts: agent-offline + replication-lag.**
`AgentHeartbeat` tracking, period+grace comparison (§6.3); `seq_no` delta
threshold (§6.4). *Accept*: depends on v3 agent/replication landing first;
acceptance criteria to be finalized alongside phase-5 implementation, not
before — flagged as blocked on that work, not schedulable independently.

**T-A12 (M) — Rule builder UI (minimal).** Rule list + create/edit form
(§9); channel management with masked secrets; test-send button. *Accept*:
an admin can create a webhook rule end-to-end (glob + event types +
throttle + channel) through the UI and receive a real test notification,
with no plaintext secret ever visible in browser devtools network tab on
a subsequent GET.

**T-A13 (S) — Recent alert-events admin view + failed-delivery surfacing.**
Extend `api/system.py` / `errors.py`-style read-only endpoints with an
alert-events listing (paginated, filterable by rule/library/delivered
status); extend Admin page. *Accept*: a permanently-failed dispatch (all
retries exhausted) is visible in this view with its sanitized error,
mirroring the existing `failed_jobs`/`failing_items` pattern.

**T-A14 (S) — Retention/purge for delivered alert_events.** Periodic purge
task, default 30-day retention for `delivered = true` rows, configurable.
*Accept*: rows older than retention window are purged on schedule;
undelivered rows are never purged regardless of age (never silently drop a
pending alert).

*Sizing key*: S = <1 day, M = 1-3 days, L = >3 days / multi-agent-session,
consistent with how prior phases (T1-T11) in this project have been sized.

---

## 11. Open questions

1. **Group-by vocabulary extensibility.** §2.6 deliberately fixes
   `group_by` to `{event_type, library_id, rule_id}` for v1 simplicity.
   Should this become user-extensible (arbitrary metadata fields, à la
   Alertmanager's free-form label matching) once custom metadata fields
   (`future-roadmap.md` §7) land? Recommend revisiting only after §7 ships,
   not speculatively now.
2. **Apprise dependency risk ownership.** Given Apprise's ~150-service
   transitive dependency tree (§1.1), should Filearr pin Apprise's own
   extras tightly (e.g. `apprise[slack,discord,...]` rather than the full
   `apprise[all]`) to shrink the actual installed surface for the common
   case, and if so, which subset ships in the default optional-extra vs.
   requiring a manual `pip install` for exotic services? Needs a concrete
   inventory pass against Apprise's current `pyproject.toml` extras at
   implementation time (not done in this research pass).
3. **Inhibition (Alertmanager-style) — v1 or deferred?** §2.2 flagged
   "scan_failure suppresses extract_error_spike for the same library" as a
   real, useful refinement but explicitly optional for MVP. Confirm this
   stays out of the T-A* breakdown above (it does) and is tracked as a
   named future-roadmap follow-up rather than silently dropped.
4. **Per-rule rate-limit ceiling (abuse/misconfiguration guard).** Should
   there be a hard system-wide ceiling (e.g. "no rule may dispatch more
   than N notifications/hour regardless of matches") as a safety net
   against a misconfigured glob (e.g. `**/*`) turning a normal scan into a
   notification storm? Not in the roadmap text explicitly, but a
   reasonable reliability safeguard — recommend adding as a small
   follow-up (`FILEARR_ALERT_RULE_MAX_PER_HOUR` global default) rather than
   blocking T-A5 on it, since group-wait/digest (§4) already absorb the
   common case.
5. **Webhook private-CIDR override scope** (§7.1 point 3): is a single
   global `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS` boolean sufficient, or
   should it be a specific CIDR allowlist (e.g. "allow 192.168.1.0/24 only,
   not all of RFC1918")? A specific allowlist is more secure but more
   admin-friction; recommend starting with the boolean (simplest, matches
   "smallest thing that satisfies the requirement") and revisiting if a
   real deployment reports it's too permissive.
6. **Agent-local dispatch channel reachability detection** (§8.6): how does
   an offline/roaming agent know whether a given channel's webhook/SMTP
   target is reachable from its current network before attempting
   agent-local dispatch vs. falling back to central-on-reconnect? A naive
   "just try it, fall back to queuing on failure" is simplest but risks
   duplicate delivery (agent-local send succeeds, but the agent also queues
   it for central because it couldn't confirm success) unless the
   `dispatch_locality` field (§8.6) is authoritative rather than
   attempt-based. Recommend making `dispatch_locality` a strict admin
   choice (no auto-detection) for v3 v1, revisiting auto-detection later
   if it proves too rigid in practice.

---

## Sources consulted

- [caronc/apprise](https://github.com/caronc/apprise) — license, breadth, maintenance
- [Change Project Licensing over to MIT · Issue #47 · caronc/apprise](https://github.com/caronc/apprise/issues/47)
- [caronc/apprise-api](https://github.com/caronc/apprise-api) — REST wrapper reference architecture
- [Recyclarr notification settings](https://recyclarr.dev/reference/settings/notifications/) — Apprise usage in *arr-adjacent tooling
- [ntfy docs — integrations](https://docs.ntfy.sh/integrations/)
- [Prometheus Alertmanager vs ntfy vs Gotify — Pi Stack, 2026-05-07](https://www.pistack.xyz/posts/2026-05-07-prometheus-alertmanager-vs-ntfy-vs-gotify-self-hosted-alert-routing-guide/)
- [containrrr/shoutrrr](https://github.com/containrrr/shoutrrr) — license, Go module
- [shoutrrr package docs — pkg.go.dev](https://pkg.go.dev/github.com/containrrr/shoutrrr)
- [Grafana — Configure notification policies](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/create-notification-policy/)
- [Grafana — Notifications fundamentals](https://grafana.com/docs/grafana/latest/alerting/fundamentals/notifications/)
- [Grafana — Mute timing vs. silences](https://grafana.com/blog/mute-timing-vs-silences-in-grafana-alerting-how-to-pick-the-best-fit-for-your-use-case/)
- [Prometheus — Alertmanager docs](https://prometheus.io/docs/alerting/latest/alertmanager/)
- [Alertmanager — grouping and aggregation, DeepWiki](https://deepwiki.com/prometheus/alertmanager/3.2-grouping-and-aggregation)
- [Alertmanager — silences and inhibitions, DeepWiki](https://deepwiki.com/prometheus/alertmanager/5-silences-and-inhibitions)
- [healthchecks/healthchecks](https://github.com/healthchecks/healthchecks) — BSD-3-Clause, period/grace model
- [Healthchecks.io docs](https://healthchecks.io/docs/)
- [changedetection.io GitHub](https://github.com/dgtlmoon/changedetection.io) — Apache-2.0, Apprise usage
- [changedetection.io plugin system, DeepWiki](https://deepwiki.com/dgtlmoon/changedetection.io/6.3-plugin-system-and-extensibility)
- [aiosmtplib on PyPI](https://pypi.org/project/aiosmtplib/) — MIT, maturity
- [OWASP — SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Webhook Security Best Practices, DEV Community 2025-2026](https://dev.to/digital_trubador/webhook-security-best-practices-for-production-2025-2026-384n)
- [Svix — Webhook Security Best Practices](https://www.svix.com/resources/webhook-best-practices/security/)
- [zitadel/zitadel #12326 — outgoing-HTTP denylist blocks RFC1918](https://github.com/zitadel/zitadel/issues/12326)
- [Time-based retention strategies in Postgres — sequin blog](https://blog.sequinstream.com/time-based-retention-strategies-in-postgres/)
- [Hatchet — Use Postgres for your events table](https://hatchet.run/blog/postgres-events-table)
- [PostgreSQL pgcrypto docs](https://www.postgresql.org/docs/current/pgcrypto.html)
- [Crunchy Data — Data Encryption in Postgres: A Guidebook](https://www.crunchydata.com/blog/data-encryption-in-postgres-a-guidebook)
- [Jinja2 autoescape-false, CodeQL query help](https://codeql.github.com/codeql-query-help/python/py-jinja2-autoescape-false/)
- [Secure Templating with Jinja2 — SSTI and Sandbox Environment, Medium](https://techtonics.medium.com/secure-templating-with-jinja2-understanding-ssti-and-jinja2-sandbox-environment-b956edd60456)
- [Paperless-ngx — Configuration docs](https://docs.paperless-ngx.com/configuration/)
- [Procrastinate — Define a retry strategy on a task](https://procrastinate.readthedocs.io/en/stable/howto/advanced/retry.html)
- [Procrastinate — Launch a task periodically](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cron.html)
- [Procrastinate — Retry stalled jobs](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html)
- Internal: `D:\repos\filearr\CLAUDE.md`, `docs/future-roadmap.md` §6, `backend/filearr/tasks/scan.py`, `backend/filearr/tasks/move.py`, `backend/filearr/errors.py`, `backend/filearr/api/system.py`, `docs/research/phase-2-indexing-controls.md` (pathspec decision), `docs/research/phase-5-distributed-agents.md` (outbox/seq_no shape)
