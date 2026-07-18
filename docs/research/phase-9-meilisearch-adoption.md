# Research Brief — Future Roadmap Item 8: Meilisearch Feature Adoption Plan

Scope: `docs/future-roadmap.md` §8 (Meilisearch feature adoption plan). This brief
turns the existing adopt-now/adopt-later/not-applicable triage (verified against
v1.48.3, dated July 2026) into implementation-ready specs, against the meilisearch
Python SDK actually pinned (`meilisearch-python-sdk==7.2.3`, confirmed in
`backend/pyproject.toml`) and the current live projection in `backend/filearr/search.py`
and `backend/filearr/tasks/index_sync.py`. Constraint order for every tradeoff below:
**security > integrity > reliability > speed > compatibility > scalability**.
AGPL-3.0-or-later — every dependency/feature is license-checked. Meilisearch stays a
**disposable projection** (invariant 1) — nothing in this brief may make it a store of
record. Researched 2026-07-07.

Reconciled against:
- `docs/research/phase-3-search-findability.md` (§5 scope: hybrid/vector, facet
  search, recency ranking, geo) — that brief owns the *findability feature* design;
  this brief owns the *operational/infrastructure* adoption of Meilisearch platform
  capabilities. Facet search is specified here (§2c) at the settings/API level;
  phase-3 owns the tag type-ahead UX built on top of it. No duplication of the
  hybrid/embedder/Hannoy findings — deferred to phase-3, referenced only where an
  operational concern (index-swap, task webhooks) intersects.
- `docs/research/phase-6-identity-auth-rbac.md` (§1.1, §2.2: tenant tokens, ACL
  compilation) — that brief owns per-session JWT/tenant-token *design*; this brief
  confirms the tenant-token feature itself is unaffected by anything shipped since
  1.48 and re-checks the CVEs it already cites (§1 below).

---

## 1. Current Meilisearch release vs. the 1.48.3 pin

**Finding: the roadmap's version pin research undershoots what has actually shipped.**
The public docs OpenAPI spec (fetched today, `meilisearch.com/docs/reference/api/*`)
is stamped `version: 1.48.1`, but Meilisearch's own blog posts confirm **shipped work
well past that tag as of today (2026-07-07)**:

- **March 2026 recap** (`meilisearch.com/blog/March-2026-updates`, published
  2026-03-12) already references v1.36 (new `attributeRank`/`wordPosition` ranking
  rules), v1.37 (replicated sharding in-engine, Hannoy stabilized — legacy
  `vectorStoreSetting` **permanently removed**, dumpless auto-migration), v1.38
  (embedding indexing perf, fixed `connection reset by peer` on remote embedders,
  fixed task-deletion edge case), and v1.39 (experimental `foreignKeys` cross-index
  hydration) as already-shipped, GA (non-Cloud-exclusive except where marked
  Enterprise).
- **April 2026 Launch Week wrap-up** (`meilisearch.com/blog/launch-week-q1-2026-wrap-up`,
  2026-04-20) confirms: self-serve sharding & replication in Cloud (Enterprise, day
  1); Enterprise SSO/SAML + SCIM lifecycle, plus **free MFA for every plan** (day 2);
  Chat UI point-and-click in Cloud dashboard (day 3); Search Performance Inspector
  exposing per-stage query timing, `showPerformanceDetails` API param (day 4);
  experimental **Document Joins** (`_foreign` filter keyword — filter one index by a
  property of documents in a linked index) and experimental **Dynamic Search Rules**
  (query-triggered pinning, `/dynamic-search-rules` route) (day 5).
- **Reference docs confirm additional shipped surface not in the roadmap's original
  pass**: a **`/webhooks` API route** (distinct from the single instance-level
  `--task-webhook-url`, up to 20 webhooks/instance), a native **`POST
  /indexes/{index_uid}/compact`** endpoint with a first-class `indexCompaction` task
  type, and `databaseSize`/`usedDatabaseSize` fields in `/stats` for measuring
  fragmentation.
- **A previously-unflagged CVE**: `meilisearch.com/blog/CVE-update-Jan-2026`
  (2026-01-27) documents an **authenticated, blind SSRF vulnerability** affecting
  self-hosted v1.8–v1.34.0, fixed in **v1.34.1** (forbids requests to non-global IPs
  per IANA special-purpose registries; opt back in via
  `--experimental-allow-ip-networks`). **This is fully moot for the 1.48.3 pin**
  (well past the fixed version) but it was not previously catalogued in
  CLAUDE.md/roadmap alongside the tenant-token CVEs (CVE-2026-57823/4) — worth a
  one-line addition to CLAUDE.md's gotchas or the roadmap's CVE list for completeness,
  since the next version bump should re-check the full CVE list, not just the two
  previously tracked.

**What this means for the pin decision:** the underlying triage in roadmap §8
(pin ≥1.48.2 for the tenant-token CVEs) remains correct as a *floor*, but "1.48.3 is
current" is now stale framing — Meilisearch's train has moved to at least v1.39 GA
(non-Cloud) with Cloud-exclusive Enterprise features beyond that. **Recommendation:
bump the pin to the latest stable 1.x tag confirmed compatible with
meilisearch-python-sdk 7.2.3 at implementation time** (verify via
`GET /version` against a throwaway container — do not trust blog-post version
numbers as the definitive tag list; confirm against
`github.com/meilisearch/meilisearch/releases` directly, since this research pass
found the GitHub releases page itself paginates non-trivially and blog posts
sometimes cite version numbers ahead of what a `docs.rs`-style OpenAPI snapshot
reflects). At minimum, treat **v1.39.x as the practical floor** for this adoption
plan, since it is the newest version blog-confirmed GA and referenced by name.

**Dumpless upgrade — still correctly "adopt later," with concrete mechanics now
confirmed:**
- Available v1.12 → v1.13+, but **still tagged experimental** as of the current docs
  pull (no GA promotion found). Requires `--experimental-dumpless-upgrade` flag.
- Mechanics: stop instance → install new binary → relaunch with the flag → Meilisearch
  auto-creates an `upgradeDatabase` task (processed immediately, **not cancelable
  once past a certain point**, blocks new task processing but **not** search queries
  during the upgrade) → poll `GET /tasks?types=upgradeDatabase`.
- **Mandatory pre-step per Meilisearch's own docs: create a snapshot first** ("it may
  in rare occasions partially fail and result in a corrupted database"). Rollback
  path: cancel the `upgradeDatabase` task (auto-rolls-back if caught before failure);
  if it already failed, or if upgrading from ≤v1.14, must restore from the
  pre-upgrade snapshot instead.
- **Filearr implication**: because Meili is disposable (invariant 1), Filearr's
  actual rollback story is simpler and better than Meilisearch's own documented
  procedure — skip the snapshot-first requirement entirely and instead **run
  `rebuild_index` after any version bump** as the de facto "dump," since Postgres is
  already the source of truth. This is a genuine simplification the disposable-index
  invariant buys Filearr that a typical Meilisearch deployment doesn't get for free.
  Still stop-the-world for writes during the bump (workers paused, scans paused) —
  dumpless upgrade only avoids *emptying and reloading* the index, it does not make
  the version bump a zero-downtime operation for the write path.
- **Verdict: adopt-later confirmed correct.** Given Filearr's own rebuild-from-Postgres
  safety net already achieves what dumpless-upgrade is *for* (avoiding a slow
  dump/restore cycle), the marginal benefit of wiring dumpless-upgrade specifically
  is low, and adopting an experimental data-migration path carries real corruption
  risk for zero net gain over "stop workers, bump image, rebuild_index." Revisit only
  if dumpless upgrade reaches GA and rebuild_index time becomes a real pain point at
  much larger doc counts than the current ~1,100.

---

## 2. Adopt-now items — concrete specs

### 2a. Task webhooks (replace polling in index_sync)

**Current state:** `backend/filearr/tasks/index_sync.py`'s `sync_items` /
`rebuild_index` Procrastinate tasks call `upsert_docs`/`delete_docs` in
`backend/filearr/search.py`, which call `AsyncClient.index(...).update_documents()`
/`.delete_documents()` and **return immediately** — the SDK's async client does not
block for task completion (there's no polling today, contrary to the roadmap's
framing of "replace polling"; Filearr's actual gap is the *opposite* — it doesn't
confirm the Meili task succeeded at all, so a silent Meili-side indexing failure is
currently invisible). **Corrected framing: task webhooks add failure observability
Filearr doesn't have today, not literally "remove polling that exists."**

**API surface (confirmed from `meilisearch.com/docs/resources/self_hosting/webhooks`
and the `/webhooks` OpenAPI spec):**
- Two mechanisms exist:
  1. **Instance-level, single webhook**: `--task-webhook-url` / `MEILI_TASK_WEBHOOK_URL`
     CLI/env flag, optional `--task-webhook-authorization-header` /
     `MEILI_TASK_WEBHOOK_AUTHORIZATION_HEADER`. Set once at container start.
  2. **`/webhooks` API route** (up to 20 webhooks/instance): `POST /webhooks` with a
     `WebhookSettings {url, headers}` body, `GET /webhooks`, `GET /webhooks/{uuid}`,
     `PATCH /webhooks/{uuid}`, `DELETE /webhooks/{uuid}`. **Authorization header
     values are redacted on read** — the docs explicitly warn not to round-trip a
     `GET` response into a `PATCH` (the redacted placeholder would be sent literally
     as the new secret). Store the real secret app-side; always send it explicitly
     on `PATCH`.
- **Payload**: `POST` to the configured URL with an **ndjson** body (one task JSON
  object per line, gzip-compressed), fired once per finished **batch** (not per
  task) — fields: `uid`, `batchUid`, `indexUid`, `status`
  (`succeeded`/`failed`/`canceled`), `type` (`documentAdditionOrUpdate`, etc.),
  `canceledBy`, `details`, `duration` (ISO-8601), `enqueuedAt`/`startedAt`/
  `finishedAt`.
- **Private-network access**: webhook receiver at `http://app:8000/...` inside the
  compose network requires `--experimental-allowed-ip-networks` (or the equivalent
  env var) covering the compose bridge subnet — Meilisearch's SSRF fix (§1, v1.34.1)
  blocks requests to non-global/private IPs by default, and this applies to the
  webhook target too, not just user-configured embedder URLs. **This must be
  explicitly allow-listed in `docker-compose.yml`** or the webhook silently never
  fires (confirm via `/webhooks` GET that the entry exists but check Meilisearch
  logs for delivery errors — no error surfaces to the Filearr side automatically).
- **Auth of the webhook receiver (Meili → Filearr callback)**: use the
  `Authorization` header mechanism (`--task-webhook-authorization-header
  "Bearer <shared-secret>"` or per-webhook `headers: {Authorization: "Bearer ..."}`
  via the API route) — a long, random, CSPRNG-generated shared secret stored in
  `FILEARR_MEILI_WEBHOOK_SECRET` (matches the existing `FILEARR_*` env convention),
  checked with constant-time comparison on the receiving FastAPI route. **Network
  isolation**: the webhook endpoint should live on the same Docker network as
  Meilisearch (not exposed publicly) — add it to `backend/filearr/api/system.py` (or
  a new `api/webhooks.py`) as an unauthenticated-by-scope-system route gated purely
  by the shared secret (it's a machine-to-machine callback, not a user-facing
  endpoint, so it doesn't fit the existing `read`/`write`/`admin` Bearer scope model
  — matches the precedent phase-6's brief sets for distinguishing session/API-key
  principals from infra-internal calls).
- **Failure/retry semantics**: Meilisearch's webhook delivery has **no documented
  retry** — if the receiving endpoint is down or errors, the notification is lost
  (not queued for redelivery). This is the load-bearing reason webhooks **must be
  additive, not a replacement for a periodic reconciliation sweep**: keep a
  low-frequency (e.g., hourly) periodic task that diffs `Item.status = active` counts
  against Meili's `/stats` document count and re-triggers `rebuild_index` if they
  drift beyond a small tolerance, functioning as the fallback-to-polling safety net
  the roadmap called for. This is cheap (`GET /stats` is O(1)) and gives a bounded
  worst-case staleness window even if every webhook delivery is lost.
- **meilisearch-python-sdk 7.2.3 support confirmed**: `AsyncClient.create_webhook()`,
  `.get_webhooks()`, `.delete_tasks()` exist on the client (webhook management is
  client-level, not index-level — makes sense, webhooks are instance/task-queue
  scoped, not per-index). Typed model: **`WebhookCreate`** (url + optional headers
  dict) — matches the house rule (typed models, not dicts) already followed for
  `TypoTolerance` in `search.py`.

**Code sketch (`backend/filearr/search.py` addition):**
```python
from meilisearch_python_sdk.models.webhooks import WebhookCreate

async def ensure_webhook() -> None:
    s = get_settings()
    async with client() as c:
        existing = await c.get_webhooks()
        target_url = f"{s.internal_base_url}/api/internal/meili-webhook"
        if not any(w.url == target_url for w in existing.results):
            await c.create_webhook(
                WebhookCreate(
                    url=target_url,
                    headers={"Authorization": f"Bearer {s.meili_webhook_secret}"},
                )
            )
```
Receiver sketch (`backend/filearr/api/webhooks.py`, new file):
```python
import gzip, json
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()

@router.post("/api/internal/meili-webhook")
async def meili_webhook(request: Request, authorization: str = Header(None)):
    s = get_settings()
    expected = f"Bearer {s.meili_webhook_secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401)
    body = await request.body()
    # Meilisearch gzip-compresses the ndjson payload
    lines = gzip.decompress(body).decode().splitlines()
    for line in lines:
        task = json.loads(line)
        if task["status"] == "failed":
            logger.error("meili task failed", extra={"task": task})
            # trigger alert / metric increment; do NOT retry-write here —
            # the periodic reconciliation sweep (§2a) is the safety net
    return {"ok": True}
```
Note the receiver is observability-only (log + alert on `failed`); it must not
attempt to "fix" anything synchronously — that's what the reconciliation sweep is
for, keeping this endpoint simple and side-effect-free per the reliability-over-speed
ordering.

### 2b. Index-swap pattern for zero-downtime settings changes

**API mechanics (confirmed, `specs.meilisearch.dev/specifications/text/0191-...`,
`meilisearch.com/blog/zero-downtime-index-deployment`):**
- `POST /swap-indexes` with `[{indexes: ["items", "items_new"]}]` — **atomic**: all
  swaps in one call succeed or none do. A swap exchanges documents, settings, *and*
  task history between the two index names (task history references get rewritten
  too — a swap between `items`/`items_new` means all mentions of `items` in past
  tasks become `items_new` and vice versa).
- Before swapping, poll `/tasks?indexUids=items_new&types=indexCreation,
  settingsUpdate,documentAdditionOrUpdate` until all relevant tasks are `succeeded`.
- **Disk headroom**: building `items_new` alongside live `items` means **both copies
  exist simultaneously on disk** during the rebuild window — same "2x" rule the
  roadmap already notes for LMDB compaction (LMDB never shrinks in place, so this is
  the same underlying constraint, not a coincidence). At Filearr's current scale
  (~1,100 docs, ~sub-100MB index) this is trivially affordable; **flag as a sizing
  input for the ops runbook (§4)** once the corpus approaches the homelab disk
  budget.
- **meilisearch-python-sdk support**: `AsyncClient.swap_indexes()` exists (list of
  `IndexSwap`/tuple pairs) — confirmed present in the SDK's client API surface
  (general index-management operations are client-level, matching the pattern
  webhooks/tasks follow).

**Filearr's `rebuild_index` redesign around swap:**

Current `rebuild_index` (`backend/filearr/tasks/index_sync.py`) upserts directly into
the live `s.meili_index` ("items") in batches of 1000 — meaning a full rebuild is
**visible to concurrent searches mid-rebuild** (a search during rebuild can see a
partially-repopulated index, and if rebuild is triggered *because* settings changed,
users see inconsistent settings/documents until the loop finishes). This violates
nothing in the architecture invariants outright, but it's a real UX/consistency gap
swap fixes for free.

**Proposed redesign:**
```python
import uuid

@proc_app.task(queue="index", name="filearr.tasks.index_sync.rebuild_index")
async def rebuild_index() -> int:
    """Full re-projection from Postgres via a shadow index + atomic swap.

    Builds into a throwaway index, applies settings, backfills documents, then
    swaps it into place atomically — concurrent searches never see a partially
    rebuilt index. Old index is deleted after a successful swap.
    """
    s = get_settings()
    shadow_name = f"{s.meili_index}_rebuild_{uuid.uuid4().hex[:8]}"
    total = 0
    async with client() as c:
        shadow = await c.create_index(shadow_name, primary_key="id")
        await _apply_settings(shadow)  # same settings routine ensure_index() uses
        async with SessionLocal() as session:
            offset = 0
            while True:
                items = (await session.execute(
                    select(Item).where(Item.status == ItemStatus.active)
                    .order_by(Item.id).offset(offset).limit(BATCH)
                )).scalars().all()
                if not items:
                    break
                await shadow.update_documents([build_doc(i) for i in items])
                total += len(items)
                offset += BATCH
        # Wait for all shadow-index tasks to finish before swapping (avoid
        # swapping in a still-indexing shadow — atomicity of the SWAP call
        # covers the swap itself, not whether shadow's own tasks finished).
        await _wait_for_index_tasks(c, shadow_name)
        await c.swap_indexes([(s.meili_index, shadow_name)])
        await c.delete_index(shadow_name)  # now holds the OLD data post-swap
    return total
```
Key design notes:
- `ensure_index()`'s settings-application logic should be factored into a shared
  `_apply_settings(index)` helper so both the live-index bootstrap path and the
  shadow-rebuild path stay in sync (avoids settings drift between the two).
- The shadow index name embeds a random suffix so a crashed rebuild doesn't collide
  with a subsequent retry, and so a stuck/orphaned shadow index from a crashed task
  is trivially identifiable in `GET /indexes` for manual cleanup — add this cleanup
  as a periodic-task check (list indexes matching `{meili_index}_rebuild_*` older
  than N hours, delete orphans), mirroring the existing periodic
  purge/reconcile pattern already in `worker.py`.
- This does **not** change `sync_items` (the per-scan incremental sync) — swap is
  specifically for full rebuilds and settings-change rollouts, not routine
  incremental updates, which should stay cheap upserts against the live index.
- **When to use swap vs. direct settings update**: routine `FILTERABLE`/
  `SEARCHABLE`/typo-tolerance tweaks that don't require re-deriving `build_doc`
  output can still go through `ensure_index()`'s direct
  `update_searchable_attributes`/etc. calls (Meilisearch itself doesn't require a
  swap for pure settings changes — the roadmap's framing of swap as *the* mechanism
  for "zero-downtime settings changes" is more precisely: swap matters when you also
  need to *reprocess every document* against the new settings before it's safe to
  expose them, e.g. a schema change to `build_doc`, not for a settings-only tweak
  Meilisearch can apply in place).

### 2c. Facet search endpoint

**Confirmed**: facet search is **on by default per index**, no separate enablement
flag beyond the index having `filterableAttributes` configured (facet search only
works on attributes that are also filterable — `search.py`'s existing `FILTERABLE`
list already covers the fields phase-3's tag type-ahead needs, e.g. `tags`, `genre`).
Inherits the index's `typoTolerance` setting (relevant to §2e below — if `tags` is
excluded from typo-tolerance disable-list, facet search on tags stays typo-tolerant,
which is almost certainly desired for a tag type-ahead UX).

There is a **`facetSearch` boolean sub-setting per filterable attribute** (distinct
from the older global on/off) confirmed by the OpenAPI error code
`invalid_settings_facet_search` in the current spec — this allows disabling facet
search on a *specific* filterable attribute (e.g., you might want `size` or `mtime`
filterable but never facet-searchable, since faceting on a near-unique numeric field
is meaningless and wastes index-time work). Recommend explicitly setting
`facetSearch: false` for `size`/`mtime`/`year` (high-cardinality/numeric,
facet-searching them is nonsensical) and leaving it enabled (default) for
`tags`/`genre`/`media_type`/`is_sidecar` (the actual facet-search candidates).

**API shape**: `POST /indexes/{uid}/facet-search` with `{facetName, facetQuery,
filter?}` — returns matching facet values with counts, prefix-matched and
typo-tolerant, sorted lexicographically by default (`sortFacetValuesBy` setting
controls this — could be `count` instead of `alpha` if phase-3 wants "most common
tags first" type-ahead ordering, worth flagging to that brief as an open UX
parameter it should decide, not this one).

**SDK support confirmed**: `AsyncIndex.facet_search(query=None, *, facet_name,
facet_query, ...)` exists directly. No settings-model gap — this is the single
cheapest adopt-now item in the whole plan (zero new infra, one new endpoint call in
`backend/filearr/api/search.py`).

**Ownership note**: the actual tag type-ahead endpoint/UI wiring belongs to
phase-3's scope (§5 "Tag system") — this brief only confirms the underlying
Meilisearch capability is configuration-light and ready to use.

### 2d. PATCH-style document updates vs. delete-by-filter

**Current compliance check on `backend/filearr/search.py`/`index_sync.py`:**
- `upsert_docs()` → `AsyncIndex.update_documents(docs)` — this is Meilisearch's
  `PUT /indexes/{uid}/documents` under the hood (partial-update semantics per-field
  is actually `documents` route behavior: `update_documents` merges fields into
  existing docs rather than fully replacing, which is the correct choice already —
  **no change needed**).
- `delete_docs()` → `AsyncIndex.delete_documents(ids)` — deletes by explicit ID list,
  **not** delete-by-filter. This is already the safer/preferred pattern; Filearr's
  code never uses `delete_documents_by_filter` (the batching-interplay hazard the
  roadmap flags) because tombstoning always operates on a known, explicit set of
  `item.id`s pulled from Postgres first (`sync_items`'s `gone` list). **Audit
  result: `index_sync.py` already complies with the "PATCH-style / explicit-ID
  updates over delete-by-filter" best practice — no code change required, just
  document the rationale so a future contributor doesn't "simplify" this into a
  filter-based bulk delete.**
- **Why delete-by-filter is riskier**: it enqueues as a single task whose effect
  depends on the index's state *at the time the task actually runs* (asynchronously),
  not at enqueue time — if documents are added matching the filter between enqueue
  and execution, they get swept up too. Explicit-ID deletion has no such TOCTOU gap
  since the ID list is fixed at enqueue time. Recommend adding a one-line comment in
  `index_sync.py` codifying this as an intentional invariant, not an oversight.

### 2e. Per-attribute typo tolerance

**Current state**: `search.py`'s `ensure_index()` already does this correctly —
```python
await index.update_typo_tolerance(
    TypoTolerance(enabled=True, disable_on_attributes=["year", "size"])
)
```
This is confirmed as the right typed-model shape (`TypoTolerance` from
`meilisearch_python_sdk.models.settings`, matching the CLAUDE.md gotcha about typed
models over dicts).

**Gap found**: `extension` is in `FILTERABLE` but **not** in
`disable_on_attributes`, despite CLAUDE.md's own architecture notes flagging
extension/hash fields as candidates for typo-tolerance exclusion. Also relevant to
T3 (sidecar filtering): `is_sidecar`/`sidecar_of` are boolean/UUID-shaped — typo
tolerance is meaningless on them structurally (Meilisearch's fuzzy matching applies
to string tokenization, and a boolean or UUID field being "typo-tolerant" has no
sensible effect on search relevance, but leaving it un-disabled costs a small amount
of indexing-time typo-index construction for no benefit). Recommend:
```python
await index.update_typo_tolerance(
    TypoTolerance(
        enabled=True,
        disable_on_attributes=["year", "size", "extension", "mtime", "sidecar_of"],
    )
)
```
(`is_sidecar` is boolean and Meilisearch does not build typo indexes on boolean
fields at all, so it's a no-op either way — omit it to keep the list meaningful
rather than padding it.)

**Interaction with phase-3's hash-search plans**: phase-3's brief doesn't currently
list a hash field in `FILTERABLE`/`SEARCHABLE` (no `content_hash`/`quick_hash` in
`search.py` today). **Flag forward**: if/when phase-3's duplicate-detection UX
(§4 of that brief) surfaces `content_hash` as a facet/filter in the Meili
projection, it must be added to `disable_on_attributes` at the same time it's added
to `FILTERABLE` — a hex hash string is exactly the kind of field where fuzzy/typo
matching is actively harmful (two different hashes differing by one hex digit are
completely unrelated files, and typo-tolerant matching would incorrectly surface
them as near-matches). This is a cross-cutting note for whoever implements phase-3's
duplicate badge, not a change to make now.

### 2f. `searchCutoffMs` guard

**Confirmed setting**: per-index `searchCutoffMs` (error code
`invalid_settings_search_cutoff_ms` confirmed in current OpenAPI spec — setting is
live, not experimental). Caps wall-clock time Meilisearch spends on a single search
before returning best-effort results rather than exhaustive ones. No default value
found in current docs (Meilisearch's internal default is documented elsewhere as
effectively unbounded/generous — the setting exists specifically for operators who
want a hard ceiling, it does not ship pre-configured to a restrictive default).

**Recommendation for Filearr's homelab-scale deployment**: set a generous but
non-infinite guard, e.g. **1500ms**, added to `ensure_index()`:
```python
await index.update_search_cutoff_ms(1500)
```
Rationale: at Filearr's current and near-future document counts (thousands, not
millions), no legitimate query should approach this ceiling — the guard exists
purely as a circuit-breaker against a pathological filter/query combination (e.g., a
malformed `_geoRadius` or deeply nested filter expression from a future feature)
hanging a request indefinitely, which matters more once the public REST API (search
+ metadata edits) is exposed beyond a trusted single-user context. This is a
security-adjacent hygiene setting (bounding worst-case request latency prevents a
crafted-query DoS vector), consistent with the security-first constraint ordering.
**Verify the exact SDK method name** (`update_search_cutoff_ms` inferred from the
naming convention of existing `update_typo_tolerance`/`update_searchable_attributes`
calls already in `search.py` — confirm directly against the SDK's `AsyncIndex` method
list before merging, since this specific method name was not independently
re-verified against the SDK source in this pass; treat as a settings-model
call needing the same typed-model treatment if the SDK expects one).

---

## 3. Operational runbook

### 3.1 LMDB compaction — superseded by the native `/compact` endpoint

**Correction to the roadmap's framing**: §8 says "periodic swap-based compaction" as
the only lever. **This is no longer accurate** — Meilisearch now exposes a
**first-class `POST /indexes/{index_uid}/compact` endpoint** (confirmed in the
current OpenAPI spec, `indexCompaction` task type, SDK-supported via
`AsyncIndex.compact()`, documented as available "in Meilisearch v1.23.0+" per the
SDK's own docstring — meaning it predates even the 1.48.3 pin and has simply been
missing from the roadmap's research). This **replaces** the need to hand-roll
compaction via the swap mechanism for the routine case (swap is still the right tool
for a *settings/schema* change that needs full document reprocessing, per §2b; it is
the wrong tool for *pure disk reclamation*, which `/compact` now does directly).

**Mechanics**: `/compact` reorganizes the index's LMDB database, reclaiming space
marked-free-but-unreturned-to-the-OS. **Same 2x disk headroom requirement as swap**
(Meilisearch "must temporarily duplicate the database" during compaction) — this is
the same underlying LMDB constraint surfacing in both mechanisms, not two unrelated
costs to budget separately.

**Fragmentation metric — now measurable, contrary to the roadmap's "compaction
schedule TBD"**: `/stats` (or `/indexes/{uid}/stats`) exposes `databaseSize` and
`usedDatabaseSize`. **Meilisearch's own guidance: if `databaseSize /
usedDatabaseSize` exceeds ~1.3 (i.e., more than ~30% overhead/fragmentation),
compacting is likely to help.** This gives Filearr a concrete, cheap, pollable
trigger condition instead of a blind time-based schedule.

**Proposed Filearr implementation** (new periodic task alongside the existing
purge/reconcile tasks in `worker.py`):
```python
@proc_app.periodic(cron="0 4 * * 0")  # weekly, low-traffic window
@proc_app.task(queue="index", name="filearr.tasks.index_sync.compact_if_fragmented")
async def compact_if_fragmented() -> bool:
    s = get_settings()
    async with client() as c:
        stats = await c.index(s.meili_index).get_stats()
        if stats.used_database_size and stats.database_size:
            ratio = stats.database_size / stats.used_database_size
            if ratio > 1.3:
                await c.index(s.meili_index).compact()
                return True
    return False
```
This is strictly better than a blind weekly compaction (avoids the 2x-disk-headroom
cost and I/O spike when nothing is actually fragmented) and strictly better than the
roadmap's original swap-only proposal (native endpoint, no shadow-index bookkeeping
needed for this specific concern).

### 3.2 Task DB 20GiB cap + task retention

**Confirmed automatic behavior**: Meilisearch auto-manages finished-task retention —
**if finished tasks exceed 1,000,000, it automatically enqueues a `taskFlushing`
task that deletes the oldest 100,000 finished tasks.** This is a built-in safety net
requiring no Filearr-side configuration. At Filearr's homelab scale (thousands of
scan/index operations, not millions), this ceiling is extremely unlikely to be
reached organically within any reasonable operational horizon — **no proactive
action needed**, but worth a one-line note in the ops runbook so a future maintainer
doesn't build unnecessary task-cleanup automation for a problem Meilisearch already
solves.

**Manual control available if ever needed**: `DELETE /tasks` (filterable by the same
params as `GET /tasks` — `statuses`, `types`, `beforeFinishedAt`, etc.) via
`AsyncClient.delete_tasks()`. Not needed as a scheduled job today; document as
available if the auto-flush threshold ever proves too coarse for a specific
diagnostic need (e.g., manually clearing old `failed` tasks after triaging them).

### 3.3 `maxTotalHits` vs. deep paging

Meilisearch's pagination settings (`Settings.pagination.maxTotalHits`, default
1000) cap how many total hits a query can report/page through — this is a
deliberate performance guard, not a bug. **Filearr's current API
(`backend/filearr/api/search.py`, per phase-3's baseline read) uses opaque
offset-cursor pagination** — confirm this stays under the `maxTotalHits` ceiling for
any realistic homelab query (a search matching >1000 items in a personal media
catalog is already a degenerate case suggesting the query itself needs refinement,
e.g., an empty/near-empty filter). **No change recommended**: raising
`maxTotalHits` trades relevance-ranking quality/performance for exhaustive paging
depth that a single-user homelab tool doesn't need — if a user needs to "page
through everything," that's better served by a bulk export/filter-driven batch
operation against Postgres directly (which has no such cap and is the actual source
of truth) than by stretching Meilisearch's pagination past its designed-for range.

### 3.4 Memory/disk sizing guidance for ~1M docs, homelab scale

From `meilisearch.com/docs/learn/engine/storage` (confirmed current, includes a
concrete measured baseline): a 19,553-document/8.6MB-raw-JSON dataset (the official
movies.json test set) produced a **224MB on-disk LMDB size and ~305MB RAM usage**
after indexing — roughly a **25x on-disk expansion factor** over raw JSON size for
that dataset shape (varies by field count/settings/language mix; Meilisearch
explicitly disclaims any universal size-prediction formula). Extrapolating
**loosely** (not a guarantee, per Meilisearch's own caveat) to Filearr's ~1M-doc
homelab ceiling: if per-document overhead is roughly comparable to the reference
set's ~11.5KB-on-disk/doc, **budget roughly 10-15GB of disk for a 1M-document
Filearr index**, with headroom for the 2x-during-compact/swap requirement (§2b/§3.1)
meaning **~20-30GB of *available* free disk** is the real operational floor, not
just the steady-state index size. RAM: Meilisearch documents that performance stays
acceptable even at a **1/3 or even ~1/10 RAM-to-disk ratio** (not requiring the full
working set resident) — a homelab box with even 2-4GB free RAM should perform
adequately for a 1M-doc index, with NVMe SSD storage strongly preferred over
HDD/NFS-backed volumes for latency reasons (directly relevant to Filearr's
CLAUDE.md-documented SMB-mounted-media deployment pattern — **the Meili data
volume itself should NOT live on the same slow SMB/rclone mount as scanned media;
it must be on local/fast storage**, which the current `docker-compose.yml`
presumably already does via a local named volume — worth a one-line confirmation
check, not a re-architecture).

### 3.5 Backup stance: confirmed — no dump/snapshot needed, by design

**Explicitly confirming the "why" for the record**: Meilisearch offers two backup
mechanisms (snapshots — `POST /snapshots`, and dumps — `POST /dumps`), both used
internally by Meilisearch's own dumpless-upgrade safety net (§1). **Filearr's
architecture invariant 1 (index is disposable, rebuildable from Postgres) makes
both mechanisms structurally unnecessary for Filearr's own backup story**:
Postgres already has its own backup posture (out of this brief's scope — a
Postgres-level concern, not a Meilisearch one), and `rebuild_index` is strictly
faster/simpler than "restore a snapshot, hope its schema still matches the running
Meilisearch version" for Filearr's specific case, because a Postgres-driven rebuild
is always correct-by-construction against the *current* `build_doc` schema, whereas
a restored snapshot/dump could carry stale document shape from before a `build_doc`
change shipped. **Explicit non-recommendation, stated for institutional memory**:
do not add scheduled Meilisearch snapshot/dump jobs to Filearr's ops story — doing
so would be *reintroducing* Meilisearch as a quasi-store-of-record (a snapshot
operators might reach for during an incident instead of the correct
`rebuild_index` path), which directly undermines invariant 1. The one exception
already covered: Meilisearch's *own internal use* of snapshots during a
self-hosted dumpless-upgrade attempt (§1) is fine, since that's transient
(deleted/superseded immediately after a successful upgrade) and never becomes part
of Filearr's durable backup posture.

### 3.6 Upgrade procedure (concrete steps for Filearr)

1. Check current running version: `GET /version` against the live container.
2. Check target version's release notes on
   `github.com/meilisearch/meilisearch/releases` directly (not blog posts — this
   research pass found blog-post version citations can reference Cloud-only rollout
   dates that don't map 1:1 to OSS release tags) for breaking changes to settings
   schema, since `search.py`'s `ensure_index()` re-applies full settings on every
   boot and a removed/renamed setting key would surface as a hard error there.
3. Pause the Procrastinate worker (stop consuming the `index` queue) to avoid races
   during the bump.
4. Bump the pinned image tag in `docker-compose.yml`, redeploy.
5. Run `rebuild_index` manually (via the admin API or a one-off script) rather than
   trusting whatever documents happen to already be in the index — this is Filearr's
   disposable-index-native substitute for Meilisearch's own dump/snapshot-based
   upgrade safety net (§3.5), and costs little at current scale (~1,100 docs).
6. Resume the worker; verify `/api/stats`-surfaced Meili health (§4) reports green
   and document counts match Postgres's active-item count.
7. Only adopt `--experimental-dumpless-upgrade` if/when it reaches GA and
   `rebuild_index` time becomes materially painful at a much larger corpus size than
   today's — not before, per §1's dumpless-upgrade analysis.

---

## 4. Monitoring additions to `/api/stats`

CLAUDE.md references a T8-established pattern for surfacing Procrastinate queue
depth into Filearr's own `/api/stats`; the same shape should extend to Meilisearch.

**Cheap and useful, adopt now (all synchronous, low-cost calls):**
- **`GET /health`** — binary up/down; poll on every `/api/stats` request or on a
  short-interval background check, surface as `meili.healthy: bool`.
- **`GET /stats`** (instance-wide) and **`GET /indexes/{uid}/stats`** — document
  count (cross-check against Postgres active-item count, §3.6 step 6), `isIndexing`
  (bool — useful to show "index catching up" in the admin UI during a large scan),
  `databaseSize`/`usedDatabaseSize` (feeds the compaction trigger, §3.1, and is
  cheap to also expose read-only in `/api/stats` for operator visibility).
- **`GET /tasks?statuses=failed&limit=1`** (or similar tight query) — surfaces
  "any recent Meili task failures" as a boolean/count in `/api/stats`, complementing
  the webhook-driven failure logging (§2a) with a pull-based check that works even
  if webhook delivery was itself lost (defense in depth, matching the reliability
  ordering).

**Experimental Prometheus metrics endpoint — do not adopt yet:**
- Confirmed **still experimental** (`--experimental-enable-metrics` flag, gated
  behind `metrics.get` API-key action). Meilisearch's own experimental-feature
  policy states the API/behavior "can break between two minor versions" — a stronger
  instability guarantee (or lack thereof) than the other experimental features
  discussed in this brief.
- A previously-reported operational failure mode is directly relevant to a homelab
  deployment: **users have reported the metrics endpoint's cardinality/volume
  overloading their own Prometheus server** (partially mitigated upstream per
  GitHub issue #4619, but not eliminated). For a project with **no existing
  Prometheus/Grafana stack** (nothing in CLAUDE.md's stack list mentions
  Prometheus), standing one up **solely** to consume an experimental, potentially
  volume-heavy metrics endpoint is a disproportionate new-infrastructure cost for
  a feature whose main value (search latency histograms, index-operation timing)
  is now **substantially covered by the GA'd `showPerformanceDetails` search
  parameter** (§1, Day 4 Launch-Week feature — per-query performance breakdown
  without any Prometheus scraping infrastructure at all).
- **Recommendation: skip the Prometheus metrics endpoint entirely for now.**
  Cover the same operational need with the cheap polling calls above plus
  `showPerformanceDetails` on ad-hoc slow-query investigations. Revisit only if
  Filearr's v3 roadmap (central console, multi-instance fleet monitoring) makes a
  real Prometheus/Grafana stack worthwhile for reasons beyond Meilisearch alone —
  at which point scraping this endpoint becomes a marginal add-on to
  already-justified infrastructure, not a new cost center on its own.

---

## 5. Risk review — confirm NOT-adopt list unchanged, flag anything newly gated

- **Sharding/"network" topology**: confirmed **still BUSL-1.1 Enterprise-only** for
  self-hosted; Cloud's new self-serve sharding/replication (§1, Day 1 Launch Week) is
  explicitly a **Cloud product feature**, not a change to the self-hosted OSS
  license boundary. The core engine license is still confirmed **MIT** (verified via
  the OpenAPI spec's own `license: {name: MIT}` field, and phase-3's brief
  independently confirms the same). **No change** — single-node self-hosted remains
  the correct, license-compliant fit for Filearr's homelab deployment model, and the
  practical ~2TiB single-index ceiling the roadmap already notes is unaffected.
- **Enterprise SSO/SAML + SCIM**: confirmed **Cloud/Enterprise-only** (Day 2 Launch
  Week feature is explicitly framed as an IdP-integration product for Meilisearch's
  own Cloud console accounts, i.e., managing who can log into *Meilisearch Cloud's
  own dashboard* — completely orthogonal to Filearr's own application-level
  auth/RBAC, which phase-6's brief already correctly scopes as an app-side,
  Meilisearch-has-no-native-RBAC problem). **No change** — this was never something
  Filearr would consume even if self-hosted-available, since it authenticates
  access to Meilisearch's own admin surface, not Filearr's end users.
- **Newly-gated since 1.48 worth flagging**: the OpenAPI spec's error code
  `requires_enterprise_edition` (present in the current schema) confirms Meilisearch
  has been steadily adding **Enterprise-gated feature checks directly into
  self-hosted error responses** (not just a Cloud-side distinction) — meaning some
  settings/routes will now return this specific error if attempted against a
  self-hosted non-Enterprise instance. **Action for implementation time**: when
  wiring any of the adopt-now features in §2, watch for `requires_enterprise_edition`
  responses specifically — none of the features specified in this brief (webhooks,
  swap, facet search, compact, typo tolerance, search-cutoff) are expected to trigger
  it based on current docs, but this is exactly the kind of gate that can move
  between minor versions, so it's cheap insurance to check for this error code
  explicitly in the SDK wrapper calls (`search.py`) and surface it clearly rather
  than letting it appear as a generic API error.
- **Document Joins / Dynamic Search Rules (new, experimental, discovered this pass)**:
  not previously on the roadmap's radar (shipped after the original triage).
  **Recommendation: not-applicable for now, re-evaluate later** — Document Joins
  (`_foreign` filter) would only matter if Filearr ever splits `items` across
  multiple indexes (e.g., one index per media type), which the roadmap explicitly
  notes Filearr doesn't currently need (single-index-per-instance design, per
  phase-3's brief). Dynamic Search Rules (pinning) has no obvious Filearr use case
  (there's no merchandising/promotion concept in a personal file catalog) — skip
  entirely unless a future feature (e.g., "pin a recently-edited item to the top of
  search temporarily") emerges as a real ask.

---

## 6. Task breakdown (T-numbered, ordered by value/risk)

| # | Size | Task | Accept criteria |
|---|---|---|---|
| **T-M1** | S | Add `facetSearch: false` to `size`/`mtime`/`year` filterable-attribute settings; extend `disable_on_attributes` to include `extension`/`mtime`/`sidecar_of` (§2c, §2e) | `ensure_index()` applies both without error against pinned Meili version; a facet-search call against `tags` still returns typo-tolerant matches, a filter query against `size` shows no typo-tolerance-induced false matches |
| **T-M2** | S | Add `searchCutoffMs` guard (§2f) | Setting applied via `ensure_index()`; confirm exact SDK method name against installed SDK version before merge; a deliberately pathological filter query returns within the cutoff bound rather than hanging |
| **T-M3** | S | Add one-line code comment in `index_sync.py` documenting the explicit-ID-deletion-over-filter-deletion rationale (§2d) | Comment present; no functional change (audit already passed) |
| **T-M4** | M | Implement `compact_if_fragmented` periodic task using `/stats` ratio + `AsyncIndex.compact()` (§3.1) | Task runs on a weekly cron, no-ops when ratio ≤1.3, triggers `compact()` and completes without error when a deliberately-fragmented test index exceeds the ratio |
| **T-M5** | L | Redesign `rebuild_index` around shadow-index + atomic swap (§2b) | A rebuild triggered mid-search shows zero query-visible inconsistency (verified via a concurrent-search test during rebuild); old shadow indexes older than N hours are cleaned up by a periodic sweep; settings applied to shadow match `ensure_index()`'s live settings exactly (shared `_apply_settings` helper, no drift) |
| **T-M6** | M | Wire task webhooks: `ensure_webhook()` bootstrap call, `/api/internal/meili-webhook` receiver route, shared-secret auth, compose network allow-list for the webhook target IP range (§2a) | Webhook registered idempotently on boot; a deliberate `failed` task (e.g., malformed document) triggers a logged alert via the receiver; receiver rejects requests with a wrong/missing bearer secret; webhook delivery works across the compose bridge network without needing `--experimental-allow-ip-networks` opened wider than necessary |
| **T-M7** | S | Add hourly reconciliation-sweep periodic task (Postgres active-item count vs. Meili doc count) as the webhook fallback safety net (§2a) | Task detects and corrects a deliberately-induced drift (e.g., manually delete a doc from Meili out-of-band) within one sweep interval |
| **T-M8** | S | Extend `/api/stats` with `meili.healthy`, `meili.document_count`, `meili.database_size`/`used_database_size`, `meili.recent_task_failures` (§4) | All fields present in `/api/stats` response; values match direct `GET /health`/`GET /stats`/`GET /tasks` calls against the same instance |
| **T-M9** | S | Bump Meilisearch pin from 1.48.3 to the actual current stable release, re-verified directly against `github.com/meilisearch/meilisearch/releases` (not blog posts) at implementation time (§1) | Pin updated in `docker-compose.yml`/deployment scripts; `GET /version` on the redeployed instance matches the new pin; `rebuild_index` run post-upgrade succeeds; full settings re-application via `ensure_index()` succeeds with no `invalid_settings_*`/`requires_enterprise_edition` errors |
| **T-M10** | S | Add a CLAUDE.md gotchas-section line noting the SSRF CVE (v1.34.1) history alongside the existing tenant-token CVE note, for future version-bump completeness (§1) | Line added; matches existing gotchas-section tone/format |
| **T-M11** | S | Confirm Meilisearch's data volume in `docker-compose.yml` is on local/fast storage, not the SMB/rclone media mount (§3.4) | Explicit confirmation documented (or a fix, if the current config is wrong); no code change expected if already correct |

**Explicitly deferred (not tasked, per §1/§5 analysis):** dumpless-upgrade wiring,
Prometheus metrics endpoint integration, Document Joins, Dynamic Search Rules,
sharding/replication, Enterprise SSO/SCIM.

---

## 7. Open questions

1. **Exact current Meilisearch OSS tag**: this research pass could not pin down a
   single authoritative "latest stable self-hosted tag as of 2026-07-07" — the
   public docs OpenAPI spec is stamped 1.48.1, but Meilisearch's own blog content
   references v1.36-v1.39 as already-shipped (dates in March 2026, before today).
   These are not necessarily contradictory (docs-site OpenAPI snapshots can lag
   behind the latest tag's actual shipped surface, or the version string in that
   YAML metadata block may not be actively maintained per-release), but **this must
   be resolved by directly querying `GET /version` against a freshly-pulled
   `meilisearch:latest` container**, not by further web research, before T-M9 is
   executed — web-search-based version reconnaissance has hit a ceiling of
   confidence here and the ground truth is one Docker pull away.
2. **`update_search_cutoff_ms` exact SDK method name** (T-M2) — inferred from
   naming convention, not independently confirmed against the SDK's method list in
   this pass. Low risk (worst case: an `AttributeError` caught immediately in
   development), but flagged rather than asserted as fact.
3. **`sortFacetValuesBy` for tag type-ahead** (§2c) — whether phase-3's tag
   type-ahead wants alphabetical (default) or by-count ordering is a UX decision
   this brief surfaces but does not resolve; belongs to phase-3's scope.
4. **Actual on-disk expansion factor at Filearr's real document shape** — §3.4's
   sizing guidance extrapolates from Meilisearch's own movies.json reference
   benchmark, which has a different field/text shape than Filearr's documents
   (file metadata + tags vs. movie plot summaries). The live test deployment
   (CLAUDE.md: 1,099+ docs indexed already) is a better empirical source — **action:
   pull actual `databaseSize`/document-count from the live deployment's `/stats`
   endpoint and compute Filearr's own real expansion factor**, replacing the
   generic estimate in §3.4 once that number is available, rather than trusting the
   movies.json analogy indefinitely.
5. **Webhook delivery across the Docker Compose bridge network** — the
   `--experimental-allowed-ip-networks` CIDR needed depends on the actual compose
   network's subnet, which isn't fixed until `docker-compose.yml`'s network block is
   inspected at implementation time; T-M6's accept criteria assumes this is
   determinable but the specific CIDR value is left as an implementation detail, not
   pre-specified here.
