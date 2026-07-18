# Research Brief — Roadmap Item 10: File Transfer & Existence Verification Through Distributed Agents

Companion to `docs/research/phase-5-distributed-agents.md` (hub-and-spoke,
mTLS, outbox replication, local SQLite index) and
`docs/research/phase-6-identity-auth-rbac.md` (RBAC, `download` as a
grantable path-scoped action). Scope: a user finds, in central search, a
file that physically lives only on a remote agent's machine, and needs to
(a) confirm it still exists/is unchanged and (b) retrieve it into the
browser. Priority order (project invariant, unchanged): **security >
integrity > reliability > speed > compatibility > scalability.**

Hard constraints inherited from phase-5:
- Agents hold **outbound-only** connections to central (mTLS batch POST for
  replication; ETag poll + opportunistic held-open SSE for config). Never
  directly reachable — not from central, not from a browser.
- Central's media mounts are **read-only** (invariant 6); this brief's
  staging area is ordinary writable central disk, not a mount, so no
  conflict.
- No column currently records which agent hosts a given `items` row (gap
  closed in §2.1).
- No general "send this agent an ad hoc instruction" channel exists yet.
  Phase-5's config channel is policy-content-only; its own research notes
  osquery's `distributed_interval` as precedent for keeping "policy" and
  "on-demand command" as separate channels even when they share transport.
  This brief needs the second channel.

---

## 1. Transfer topology

**Relay-through-central (MeshCentral-style live pipe).** MeshCentral's
relay pipes bytes browser↔agent through the server without buffering,
"the server is performing a TLS decrypt and re-encrypt... traffic cost is
high" — and it requires the agent to be actively, simultaneously streaming
for the whole transfer. Filearr's agents are poll/backoff clients, not
always-on relay endpoints; a live pipe has no natural resumability (a
dropped tab or agent reconnect mid-stream loses everything). Poor fit for
multi-GB video under reliability-over-speed.

**Agent-hold-open / long-poll pickup.** Fleet's precedent: "Fleet notifies
the host that it wants new information, then waits for the host to
respond... on the interval it checks into Fleet" (fleetdm/fleet#519) — no
push, the host's own check-in carries the instruction. Right shape for
**dispatching** a transfer job, but says nothing about where the bytes
land.

**WebRTC/hole-punching.** Rejected as overkill. Even Tailscale, the
project most invested in P2P NAT traversal, ships a mandatory DERP relay
fallback because "symmetric NAT, carrier-grade NAT, and restrictive
corporate firewalls routinely defeat classical hole-punching." Syncthing
(a genuinely different mesh model) also falls back to relays. Standing up
STUN/TURN/ICE plus a signaling path through central is real complexity for
no offsetting benefit at Filearr's self-hosted/LAN-adjacent scale —
matches phase-5 §4.4's own calibration against sketch-based reconciliation
for the same reason.

**Central object-staging.** Agent uploads once, on its own poll cadence,
to a TTL'd central staging area; browser downloads independently, at its
own pace. Decouples the two legs in time — the single most important
property for reliability with large files. Mirrors the pattern the
replication outbox already uses (agent pushes when it can; central is
durable storage in between).

**Recommendation: central object-staging**, job dispatched via a new
agent-command poll channel (§3) reusing Fleet's notify-then-check-in
pattern, data plane on resumable chunked upload (§2), not a live pipe.
Reuses phase-5's existing mTLS channel unchanged (no new transport, no
listening port on the agent, no NAT traversal). Cost: temporary central
disk proportional to in-flight retrievals, bounded via §2.5/§6.2, not
open-ended.

---

## 2. Streaming mechanics

### 2.1 Gap: which agent hosts this item

Phase-5's `agent_replication_log` records history, not current ownership.
Add:

```sql
ALTER TABLE items ADD COLUMN agent_id UUID REFERENCES agents(id);
-- NULL = centrally-scanned (existing direct-mount download, unaffected).
-- NOT NULL = agent-hosted; only reachable via this pipeline.
CREATE INDEX ix_items_agent_id ON items (agent_id) WHERE agent_id IS NOT NULL;
```

Set by phase-5's `apply_batch` (P5-T4) in the same upsert transaction —
additive to an existing write path. Flagged as a real, previously
unaddressed dependency on P5-T4.

### 2.2 Chunked upload resumability

**tus protocol** is production-mature ("ready for use in production...
feedback from Vimeo, Google") with a Go reference server (`tus/tusd`), a
full Go client (`tus/tus-go-client`), and Python/FastAPI-adjacent
bindings — a strong fit given phase-5's Go agent and FastAPI central.
Recommend tus (or a hand-rolled offset-`PATCH` subset if a full tus
server is judged too heavy — flagged as a sizing decision, §8 Q2) for the
**agent→central staging upload**.

For **central→browser download**, plain HTTP `Range`/`Content-Range` GET
is sufficient — browser-native resumability, no extra protocol, since this
leg reads an already-staged (or known-good-prefix) file rather than a live
write target.

Explicit non-goal: S3-multipart semantics — "its own chunk-size minimums,
its own authentication model," no S3 dependency exists in this project,
and it would be a second incompatible resumability scheme for no benefit.

### 2.3 Integrity verification end-to-end

On staging-upload completion, central recomputes a streaming content hash
(`zeebo/xxh3`'s `Hasher` implements Go's `hash.Hash`, folded into the same
write pass) and compares against the catalog's stored `content_hash`.
- **Match** → staging row verified, download becomes available.
- **Mismatch** → the file changed on the agent since its last scan. Not a
  transfer failure — stale catalog data. Do not serve the (correct)
  staged bytes under a hash that disagrees with the catalog; instead flag
  the item for re-extraction via the existing scan-diff path, and gate the
  download until the catalog is corrected. A slightly delayed download is
  an acceptable cost for never silently serving mismatched content as if
  it matched (integrity over speed).
- A corrupted **upload** (bit-flip, truncation) is caught at the same
  checkpoint; retry resumes from the last acked tus offset, reusing
  phase-5's existing exponential-backoff posture, not a from-scratch redo.

### 2.4 Bandwidth limiting and concurrency caps

Per-agent **token bucket** on the upload leg via `golang.org/x/time/rate`
(the de-facto Go standard, `Limiter.Wait`/`Reserve`/`Allow`), sized from a
`max_upload_bytes_per_sec` field added to phase-5's existing policy-poll
payload (no new delivery mechanism). Central applies an **independent**
token bucket on the download leg so one large upload doesn't stall an
unrelated download and vice versa. Concurrency: recommend 1 in-flight
staging upload per agent (a home NAS/laptop rarely benefits from
parallel transfers and risks starving its own scan/replication traffic);
a separate admin-configured global cap on simultaneous in-progress
downloads protects central disk/egress.

### 2.5 TTL'd staging cleanup

Staged files are ephemeral and trivially re-fetchable — losing one early
is an inconvenience, not data loss. A Procrastinate **periodic task**
(reusing the project's existing no-Redis periodic-purge pattern):
- deletes staging rows/files past `expires_at` (default e.g. 24h from
  completion) unless a download is **actively** in progress (checked via
  `last_range_request_at`, not just creation time, so a slow multi-GB
  download isn't cut off);
- reclaims abandoned partial uploads (no chunk activity) on a shorter TTL
  (e.g. 48h).

---

## 3. Existence / freshness verification

### 3.1 New primitive: `agent_commands`

Phase-5 has no general command channel; osquery's `distributed_interval`
(a deliberately separate interval/toggle from `config_refresh`, behind
Fleet's live-query feature) is the precedent for keeping ad hoc
instructions distinct from policy delivery. Proposed minimal table,
riding the same authenticated poll infrastructure phase-5 already built:

```sql
CREATE TABLE agent_commands (
    id           UUID PRIMARY KEY DEFAULT uuidv7(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN
                     ('stat_check', 'rehash_check', 'stage_upload')),
    item_id      UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    payload      JSONB NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','picked_up','done','failed','expired')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    picked_up_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result       JSONB,          -- {"exists", "size", "mtime", "quick_hash", ...}
    requested_by UUID REFERENCES principals(id)  -- audit actor, §4.5
);
CREATE INDEX ix_agent_commands_pending ON agent_commands (agent_id, created_at)
    WHERE status = 'pending';
```

Idempotent by construction (same posture as replication's
`(agent_id, seq_no)` upsert: re-picking-up an already-`done` row is a
no-op). TTL expiry via a periodic sweep identical in shape to §2.5's,
flipping stale `pending` rows to `expired` (not deleting — so the UI can
say "the agent never came back" rather than showing nothing).
`stage_upload` reuses this same table/poll: the "start a transfer"
instruction is dispatched exactly like a `stat_check`, keeping
existence-check and retrieve-trigger on one primitive.

### 3.2 verify-cheap vs verify-strong

- **`stat_check`** (verify-cheap): plain filesystem `stat()` on the
  resolved path (via `library.native_prefix`, invariant #3 unchanged) →
  `exists`/`size`/`mtime`. Cheap enough to run automatically on item-detail
  view, not just on request.
- **`rehash_check`** (verify-strong): additionally computes `quick_hash`
  (and, only if explicitly requested, full `content_hash`) reusing the
  scanner's existing tiered-hash strategy. A full read of a possibly
  multi-GB file — must be user-triggered, never automatic.

### 3.3 Reconciling results

Match → update only `last_verified_at` (a new, useful "last confirmed"
signal distinct from last-scan time). Mismatch (size/mtime changed, or
`exists:false`) → run the existing scan-diff logic from this new trigger
(not a new code path) and **emit an alert** on phase-8's feed — phase-8's
task list already reserves **P8-T11 "agent-offline + replication-lag
alerts"** in Wave 5; this is a natural sibling alert type, not a second
alerting system. `exists:false` tombstones via the same `missing` status
path invariant 4 already mandates.

---

## 4. Security

**RBAC before job creation (§4.1).** `download` is already a first-class
`path_grants` action in phase-6, migrated onto `require_permission` by
P6-T4 alongside item-detail/PATCH. This brief's retrieve endpoint is that
same download endpoint's agent-hosted-item branch: `require_permission
(action='download')` must gate `agent_commands` row **creation**, before
any agent bandwidth or central disk is spent — authorization stops the
costly side effect, not clean up after it.

**Agent-side path re-validation (defense in depth).** Phase-5's threat
model places a malicious/compromised central server out of scope for
hub-and-spoke ("the central server is the trust root by construction")
while separately insisting central not blindly trust agent-reported data.
This brief reconciles both without contradiction: trusting central as the
authority for *should this transfer happen* (RBAC) is not the same claim
as trusting central to never send a malformed/out-of-scope path. Agents
should re-validate, on every picked-up command, that the resolved path
falls inside their own configured library roots before touching disk — a
free local check defending against a confused/buggy/compromised-build
central, mirroring the same "defense in depth doesn't require
relitigating the trust boundary" posture phase-5 already applies to
agent-side update-manifest signature verification.

**No direct browser↔agent connections, ever.** The browser only talks to
central (search, retrieve-trigger, staged download, SSE progress) — never
receives an agent address/port/credential. Object-staging buys this for
free; a relay-through-central design would at least require the server to
broker a session identifier.

**Staging encryption at rest.** Not recommending new work: staged files
inherit whatever disk-level protection central storage already has
(LUKS/BitLocker/ZFS, if configured) — same posture as Postgres's own data
files today, never independently encrypted at the application layer.
Document explicitly for operators rather than leaving unstated: "staged
downloads use the same disk-level protection as central storage; encrypt
the underlying volume if your compliance posture requires more."

**Audit logging: override the read-audit-off default for `download`.**
Phase-6 defaults `FILEARR_AUDIT_READS=false` reasoning that ordinary
read/search audit volume is high, value low — but phase-6's own
evaluation algorithm already treats `download` as sitting with `modify`
in a higher sensitivity tier than `search_metadata`/`search_content`.
Recommend auditing every completed retrieve **unconditionally**
(actor, item, agent, bytes transferred, verified-hash outcome),
regardless of the flag — this is "bytes off someone else's machine
leaving the system," materially different from a search-result view, and
an operator investigating a leak needs it logged by default. Narrow,
justified carve-out; does not touch the existing default for ordinary
reads.

---

## 5. UX

**Search/detail surface.** Agent-hosted items show hosting **agent name**
(`agents.name`), **online status** (derived from `agents.last_seen_at` —
"seen within N minutes," no new heartbeat), and **freshness**
(`last_verified_at`, falling back to last-scan/replication time).

**Retrieve flow.** Click Retrieve → RBAC check (§4) → create
`agent_commands` row (`kind='stage_upload'`) → open SSE stream at
`/api/items/{id}/retrieve/events`, **directly reusing the existing
`EventSourceResponse` pattern** from `/scans/{id}/events` — states
`queued → agent_picked_up → uploading (N/M bytes) → staged_verified →
ready_for_download`, or `failed`/`offline_timeout`. Once verified, browser
issues a direct Range-capable `GET /retrieve/download`; bytes never ride
the SSE channel. A separate, lightweight **Verify** action (`stat_check`)
lets a user confirm existence without triggering a transfer, reusing the
same command/SSE plumbing at a cheaper `kind`.

**Offline agent: queue-with-TTL, not immediate refusal.** Phase-5's own
state machine treats OFFLINE as "not a degraded mode... the expected,
designed-for normal condition." Refusing outright breaks the single most
realistic case (a personal laptop merely asleep). Recommend: the
`agent_commands.expires_at` (hours by default, configurable) gives the
agent a real window to reconnect on its own poll cadence; SSE shows a
clear "waiting for {agent} to reconnect" state, not a spinner or silent
hang. Lapsed TTL → `expired`, terminal `offline_timeout` SSE event, cheap
retry (a new row). Unbounded wait is rejected too — it leaves the user
with no feedback and central holding an indefinite half-open resource.

---

## 6. Architecture summary

**Transfer flow:** Browser → RBAC check → `agent_commands(stage_upload)`
row created → agent's next poll picks it up → agent re-validates path
inside its own roots (§4) → tus-PATCH chunked upload, rate-limited,
resumable → central streaming-hash-verifies against catalog (§2.3) →
staged row marked verified → browser Range-GETs from staging disk,
progress the whole way via SSE reusing `/scans/{id}/events`.

**Staging lifecycle:** pending command → agent uploads (resumable, rate
limited) → hash-verify (mismatch ⇒ scan-diff correction, no serve until
resolved) → available for Range-GET, watermarked by
`last_range_request_at` → Procrastinate TTL sweep deletes once idle past
TTL with no active download.

**Integrity/freshness semantics:**

| Check | Cost | Trigger | Updates |
|---|---|---|---|
| `stat_check` | near-free | automatic on detail view, or explicit Verify | `last_verified_at`; diff → scan-diff path |
| `rehash_check` | expensive (full read) | user-explicit only | as above + `content_hash` correction |
| Transfer-time hash verify | one extra streaming pass | every `stage_upload` completion | blocks serving on mismatch until catalog corrected |

---

## 7. Task breakdown — P10-T1..

Sequenced: command primitive → item→agent routing → verification →
staging/upload → integrity → download/SSE UX → offline handling → cleanup
→ audit → UI.

**P10-T1 — `agent_commands` table + poll endpoint — size M**
Deliverable: migration; `GET /api/agents/{id}/commands?since=...`
(mTLS-authenticated, same channel as replication/config poll); agent-side
pickup/transition logic; Procrastinate TTL sweep to `expired`.
Accept: agent picks up a command within one poll interval; duplicate
pickup of a `done` command is a no-op; an unpicked command past
`expires_at` flips to `expired` and stays queryable.
Deps: intra — none (foundation). Cross — **P5-T2** (mTLS channel),
**P5-T6** (sibling poll/ETag machinery).

**P10-T2 — `items.agent_id` column + `apply_batch` wiring — size S**
Deliverable: migration; wire into phase-5's `apply_batch` (P5-T4) so
every replicated upsert sets ownership.
Accept: items replicated from agent X show `agent_id=X`; centrally-scanned
items show `agent_id IS NULL`.
Deps: intra — none. Cross — **P5-T4** must land first or be modified in
the same change (previously unaddressed dependency, §2.1).

**P10-T3 — stat_check / rehash_check verification flow — size M**
Deliverable: RBAC-gated endpoint requesting a `stat_check`
(`search_metadata` suffices) or `rehash_check` (`download` required,
full-content-read sensitivity); agent-side stat/quick_hash/content_hash
reusing existing tiered `hashx`; central reconciliation (scan-diff reuse,
`last_verified_at`, tombstone via existing `missing` path); alert
emission on mismatch.
Accept: unchanged file updates only `last_verified_at`; changed file
updates size/mtime and alerts; deleted file tombstones via the existing
path and alerts; `rehash_check` corrects `content_hash` on mismatch.
Deps: intra — **P10-T1**, **P10-T2**. Cross — **P8-T11** (sibling alert
consumer); reuses invariant-4 tombstone path unchanged.

**P10-T4 — Staging schema + tus-based agent upload — size L**
Deliverable: `staging_transfers` table (id, item_id, agent_id,
`agent_commands.id` FK, status, `expires_at`, `last_range_request_at`,
staged path, verified hash); tus (or subset) endpoint into a writable
non-media-mount staging directory; agent-side tus-client with
token-bucket rate limiting (§2.4, policy-delivered cap) and concurrency
cap of 1 upload/agent; path re-validation against configured roots before
any read (§4, defense in depth).
Accept: a multi-GB upload survives an agent restart mid-transfer and
resumes from the last acked chunk; an out-of-root path (simulated
confused/malicious command) is refused agent-side before any file read;
rate limiting measurably caps throughput in a soak test.
Deps: intra — **P10-T1**. Cross — **P5-T4** (mirrors its outbox/backoff
posture); staging dir explicitly outside invariant 6's read-only mounts.

**P10-T5 — Integrity verification on staging completion — size S**
Deliverable: streaming hash (`zeebo/xxh3` or project's `hashx` equivalent)
folded into the upload write path; mismatch flags the item for
re-extraction via scan-diff and withholds download until corrected; match
marks the staging row verified.
Accept: matching bytes become downloadable; a simulated post-scan file
change does not become downloadable until the catalog is corrected, and
the correction is visible via existing item-history/versioning.
Deps: intra — **P10-T4**. Cross — none (reuses `hashx`/versioning).

**P10-T6 — Central download endpoint + SSE progress — size M**
Deliverable: `POST /api/items/{id}/retrieve` (RBAC `download`, creates the
command); `GET /retrieve/events` SSE reusing `EventSourceResponse`; `GET
/retrieve/download` Range-capable from verified staging; Svelte progress
component.
Accept: a full retrieve against an online agent completes end-to-end with
visible progress at each state; a Range request resumes correctly; RBAC
check is proven to run before job creation (no job exists for a principal
lacking `download`).
Deps: intra — **P10-T4**, **P10-T5**. Cross — **P6-T4**
(`require_permission` must cover `download` first).

**P10-T7 — Offline-agent queue-with-TTL behavior — size S**
Deliverable: `stage_upload` TTL default/config (longer than
`stat_check`'s); SSE `waiting_for_agent` state; terminal
`offline_timeout` event + UI messaging.
Accept: retrieve against an offline agent shows a clear waiting state, not
a spinner or immediate error; succeeds automatically if the agent
reconnects within the TTL with zero re-interaction; a lapsed TTL produces
a clear, actionable failure.
Deps: intra — **P10-T1**, **P10-T6**. Cross — none.

**P10-T8 — Staging TTL cleanup (Procrastinate periodic) — size S**
Deliverable: periodic task deleting `staging_transfers` rows/files past
`expires_at` unless actively downloading; shorter-TTL reclaim for
abandoned partial uploads.
Accept: unclaimed staged file deleted after TTL; actively-downloading file
not deleted mid-stream past nominal TTL; abandoned partial reclaimed on
its own schedule.
Deps: intra — **P10-T4**. Cross — reuses existing periodic-purge
infrastructure unchanged.

**P10-T9 — Audit logging for retrievals — size S**
Deliverable: every completed retrieve and `rehash_check` writes an audit
row regardless of `FILEARR_AUDIT_READS`.
Accept: completed retrieve produces an audit row even with the default
`false`; an ordinary search still produces none under the same default
(regression proving the carve-out is scoped correctly).
Deps: intra — **P10-T6**. Cross — **P6-T9** (extends, doesn't duplicate).

**P10-T10 — UI: agent identity, online status, inline Verify — size S**
Deliverable: item-detail (and space-permitting search-row) display of
hosting agent name/online status/freshness; standalone Verify action.
Accept: detail view correctly shows agent name and online/offline state;
Verify completes and updates freshness without a full transfer.
Deps: intra — **P10-T3**. Cross — **P5-T1** (`agents.name`,
`last_seen_at` already exist).

---

## 8. Open questions

1. **Staging disk quota.** No total-staging-bytes cap is proposed;
   likely fine unattended at expected scale, but a pathological
   many-simultaneous-large-retrieves case could fill central disk.
   Decide in P10-T4 whether a simple ceiling is worth adding now.
2. **tus vs hand-rolled offset-PATCH.** Sizing decision for P10-T4 —
   needs a short spike (mirroring phase-5's own P5-T2a/T3a pattern)
   before confident sizing.
3. **`stage_upload` TTL default value.** "Hours, not minutes" recommended
   but not pinned — needs a product decision before P10-T7 hardcodes one.
4. **Retroactive/background verification cadence.** This brief makes
   `stat_check` view/user-triggered only; whether a periodic "spot check
   N random agent-hosted items per agent per day" sweep is worth adding
   is deferred, analogous to phase-5's own reconciliation-cadence
   question but for content freshness rather than catalog completeness.
5. **`machine_group_id` ↔ retrieve-scoped grants.** Inherits phase-6's own
   unresolved open question #7 (path_grants/agent-groups join semantics)
   unchanged — needs the same joint design pass phase-6 already flagged,
   not a second independent resolution here.
6. **Partial-file streaming before full verification.** Full-file hash
   verification gates downloadability (§2.3/§6), so a user cannot start
   streaming a multi-GB video before upload+hash fully completes —
   correct under integrity-over-speed, but a real latency cost.
   Progressive block-level verification (serve already-verified prefix
   blocks while later blocks still upload) is a plausible future
   optimization, explicitly **not** recommended for the initial cut —
   revisit only if full-verify-then-serve latency proves a real
   complaint.

---

## Sources cited

- MeshCentral design docs / DeepWiki relay summary — live-relay session
  brokering and its re-encrypt cost (§1). https://docs.meshcentral.com/design/ ,
  https://deepwiki.com/Ylianst/MeshCentral/5-relay-and-remote-access
- Fleet engineering (fleetdm/fleet#519) and Fleet reference-architecture
  docs — notify-then-poll precedent (§1, §3.1). https://fleetdm.com/docs/deploy/reference-architectures
- Tailscale "How NAT traversal works" / DERP docs — hole-punching failure
  modes and mandatory relay fallback (§1). https://tailscale.com/blog/how-nat-traversal-works ,
  https://tailscale.com/docs/reference/derp-servers
- Syncthing FAQ — relay fallback and staging `.tmp` TTL precedent (§1,
  §2.5). https://docs.syncthing.net/users/faq.html
- tus.io protocol, `tus/tusd`, `tus/tus-go-client` — production maturity
  and Go/Python tooling (§2.2). https://tus.io/ , https://github.com/tus/tusd ,
  https://github.com/tus/tus-go-client
- AWS S3 multipart upload docs — rejected-alternative comparison (§2.2).
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html
- `golang.org/x/time/rate` — token-bucket API (§2.4).
  https://pkg.go.dev/golang.org/x/time/rate
- `zeebo/xxh3` — streaming `hash.Hash`-compatible XXH3 (§2.3).
  https://github.com/zeebo/xxh3
- Procrastinate docs/GitHub — periodic-task pattern (§2.5, §7).
  https://procrastinate.readthedocs.io/ , https://github.com/procrastinate-org/procrastinate
- Tactical RMM docs — MeshCentral-integrated remote file browser as
  comparable-tool context (§1). https://docs.tacticalrmm.com/
- `docs/research/phase-5-distributed-agents.md` (this repo) — §4
  replication/outbox, §6 config-push, §7 component designs, §8 threat
  model, §9 language recommendation, §10/§11 tasks and open questions —
  primary architectural constraint source.
- `docs/tasks/phase-5-distributed-agents-tasks.md` (this repo) — rulings
  R1-R6, DDL (`agents`, `enrollment_tokens`, `agent_replication_log`),
  P5-T1..T8.
- `docs/research/phase-6-identity-auth-rbac.md` (this repo) — §1.4 RBAC
  engine, §2.3 DDL (`download` action), §2.5 evaluation algorithm, §4
  security notes, §5 open questions.
- `docs/tasks/phase-6-identity-auth-rbac-tasks.md` (this repo) — rulings
  R1-R7, P6-T1..T12, specifically P6-T4 and P6-T9.
- `docs/tasks/roadmap-sequencing.md` (this repo) — Wave 5 placement and
  P8-T11 as sibling alert-feed consumer.
