# Filearr — Future Functionality Roadmap (v2/v3: Distributed Platform)

Extends the v1 single-node catalog into a centrally managed, multi-machine file
intelligence platform. Research-verified 2026-07-06 (prior-art citations in the
research transcripts; Meilisearch inventory verified against v1.48.3 releases).

---

## 1. Distributed agent architecture

**Native clients (agents)** installable on Windows/Linux/macOS, centrally
configured:
- **Enrollment:** one-time token → agent generates key + CSR → central server
  signs a short-lived client certificate, auto-renewed (**step-ca pattern**, as
  used by Fleet/osquery-style fleets). Certificate = machine identity
  (Syncthing's cert-hash-as-device-ID is the proven precedent). All
  agent↔server traffic is **mutual TLS** — authorization and integrity in one
  mechanism.
- **Config push:** central server holds per-agent/per-group index policy (what
  paths to index, type/exclusion rules, watch schedules); agents poll or hold a
  long-poll channel; policy is versioned and auditable.
- **Local-first indexing:** agents scan to a **local SQLite index** (FTS5),
  fully functional offline.
- **Replication:** transactional **outbox + per-agent monotonic sequence
  number**, batched upserts to central Postgres keyed on `(agent_id, seq_no)`
  for idempotency. No CRDTs/vector clocks needed in a hub-and-spoke topology
  (research conclusion — cr-sqlite/Litestream do NOT solve many-SQLite→one-
  Postgres). Tombstones replicate deletes; periodic full-reconciliation sweep
  as safety net.
- **Agent updates:** signed packages, central version pinning, staged rollout
  groups (Fleet/Wazuh precedent).

## 2. Local query access
- **Local CLI** (`filearr query ...`) and optional **local web UI** against the
  agent's SQLite index — answers "where did I put that file" even when the
  central server is unreachable.
- Local access is **policy-controlled from the central console**: enabled/
  disabled per machine group, optionally auth-required, read-only scope.

## 3. Identity, auth & RBAC (central console)
- **Auth providers:** local accounts, **LDAP** (`python-ldap` — ldap3 is
  stalled since 2021), **SAML** (`pysaml2`), **OIDC/SSO** (`Authlib`).
  fastapi-users is in maintenance mode — avoid.
- **RBAC, two layers** (Grafana/Portainer precedent):
  1. Global roles: Admin / User / Viewer.
  2. Group-based resource ACLs on **machine groups** and **file locations**
     (path scopes) with inheritance + override. Grantable actions:
     `search_metadata`, `search_content`, `download`, `upload`, `modify`,
     `delete`, `edit_metadata`, `manage_alerts`.
- **Enforcement in search:** ACLs compile into **Meilisearch tenant tokens**
  (per-session signed JWT with embedded filter, e.g.
  `acl_groups IN [...]`) — row-level security without trusting the client.
  Requires Meili ≥ 1.48.2 (tenant-token CVEs patched). Meilisearch has no
  native RBAC (roadmap-only) — the app layer owns the permission model.

## 4. Indexing controls
- **Inclusions/exclusions:** per-library and per-agent folder include/exclude
  (globs — already in v1 schema), plus **preset bundles** toggled with one
  click: "system files", "hidden/dotfiles", "caches/temp", "node_modules &
  build artifacts", "OS metadata (Thumbs.db/.DS_Store)".
- **Default search locations** per platform (Documents/Desktop/Downloads/
  user-defined) offered at agent setup, easily amended centrally.
- **File-type presets:** extend v1's per-library media-type toggles to
  arbitrary extension groups, centrally managed.
- **Content-sniffing for extensionless / mis-extensioned files (OPS-T4
  follow-up):** the live 750k corpus has ~3k extensionless files (`''` in the
  `unmapped_extensions` report) that carry no extension signal and stay
  `media_type=other`. A future pass could magic-sniff a bounded prefix
  (`python-magic`/libmagic, already an available dep) to classify them by
  content — but content-sniffing 3k+ files *during scan* over SMB/NFS is a real
  cost, so this is deferred: run it as an opt-in, off-scan reclassify job over
  the extensionless set only, never inline in the walk.
- **Hot-folder scheduling:** per-directory watch/scan frequency overrides —
  "watch Downloads every minute, archive shares nightly" — implemented as
  per-path Procrastinate periodic tasks; local watchfiles where the
  filesystem supports it.

## 5. Search & findability (the "where did I put that file?" feature set)
Priority-ordered from prior-art research (Everything, Recoll, sist2, Spotlight,
Paperless-ngx, Immich, 2024-26 local-AI tools):

**P0 (core payoff)**
- **Search-as-you-type filename/fuzzy matching** (already v1) with path
  breadcrumbs and **"open containing folder" / copy-path actions**.
- **File hash search:** exact-match lookup by xxh3 (stored) plus optional
  MD5/SHA-256 (computed on demand or per-policy) — filterable attribute, typo
  tolerance disabled. Answers "do I already have this file / where else is it?"
- **Snippets & highlighting** (Meili `attributesToHighlight`/`crop`) once
  content indexing lands.
- **Quick filters:** kind/size/date chips (facetStats drive range sliders).
- **Recency ranking boost** ("the file I touched last week beats the 2019 copy").

**P1**
- **Content extraction** for documents (Tika-class: pypdf/docx text, plus OCR
  via Tesseract for scans/images — Recoll/Paperless precedent; cached so OCR
  runs once). This unlocks "what files CONTAIN this information".
- **Saved searches / smart folders** (persisted queries, shareable, ACL-aware).
- **Semantic/hybrid search:** Meili hybrid mode with **local embedders**
  (huggingFace/ONNX or Ollama — never cloud for private files), **binary
  quantization from day one** (~10x smaller, required at millions of vectors);
  Hannoy backend (default ≥ v1.29) makes this practical.
- **Similar-files** ("more like this") via Meili `/similar` endpoint —
  duplicate-adjacent discovery for free once embeddings exist.
- **Duplicate awareness** surfaced in results ("3 copies exist" badge, hash-based).

**P2**
- **Timeline browsing** (created/modified date histogram navigation).
- **EXIF deep extraction** (exiftool sidecar service): camera, GPS (enables
  Meili geo filters for photo maps), lens, dimensions — extending v1's Pillow
  basics. Same pattern for extended audio/video technical metadata.
- **Tag system** with facet-search-powered type-ahead (Meili facet search
  endpoint, v1.3+) once tag cardinality grows.
- **Archive/email indexing** (zip/7z member listings; mbox/PST later —
  Recoll precedent).
- **Natural-language query assist** (query→filter translation, local LLM optional).

**P3**
- File provenance (download source URL, originating agent/machine).
- Frecency (frequency+recency) personal ranking profiles.

## 6. Alerting
- **File-change alert rules:** watch expressions (path glob + event type:
  created/modified/deleted/moved + optional hash-change) → notification
  channels (webhook, email, Apprise → Discord/ntfy/etc.), with per-rule
  throttling/digests. Runs on agent events post-replication; local evaluation
  for offline machines with delivery on reconnect.
- Operational alerts: scan failures, agent offline, replication lag,
  extract-error spikes (extends Phase-1 T11).

## 7. Data model extensions
- **Extended metadata:** keep JSONB `metadata` bag; add **typed per-domain
  schemas** (registered "metadata profiles" per file type with validation),
  and **custom user-defined fields** (central definition, per-location
  applicability) — powers both faceting and the RBAC `edit_metadata` action.
- **Customizable displays:** per-file-type card/detail templates (image →
  preview+EXIF grid; audio → waveform+tags; 3D → mesh stats+render; generic →
  key facts), plus an always-available **"Raw" tab showing every stored field**
  (core columns, extracted metadata, user metadata, sync/provenance info).
- Full-fidelity provenance columns per item: source agent, first/last seen,
  replication seq, policy version that indexed it.

## 8. Meilisearch feature adoption plan (verified against v1.48.3)
**Adopt now:** tenant tokens (RBAC); pin ≥1.48.2 (CVE-2026-57823/4); facet
search endpoint; **index-swap** pattern for zero-downtime settings changes;
**task webhooks** (replace polling in index-sync); prefer PATCH-style document
updates (delete-by-filter breaks task batching); per-attribute typo tolerance
(off for hash/extension fields); `searchCutoffMs` guard.
**Adopt later:** hybrid/vector + local embedders + binary quantization;
`/similar` endpoint; federated multi-search + `facetsByIndex` (if indexes split
per tenant/type); dumpless upgrades; geo filters (photo GPS).
**Not applicable:** sharding/"network" (BUSL Enterprise — single node covers
millions of docs; ~2TiB practical index ceiling), SSO/SCIM (Enterprise, console
identity only), waiting on native RBAC (roadmap-only — build app-side).
Operational notes: LMDB never shrinks on delete → periodic swap-based
compaction; task DB capped 20GiB; `maxTotalHits` governs deep paging.

## 9. License recommendation
Goal: **stays open source, commercial use allowed.** All OSI licenses permit
commercial use; the real question is copyleft scope:
- **GPL-3.0** forces derivatives to stay open **only when distributed** — a
  competitor may modify Filearr and run it as a closed hosted service (the
  "SaaS loophole"). For predominantly server-side software this is the main leak.
- **AGPL-3.0** closes that loophole (network use = distribution). It is the
  de-facto standard in exactly this app category: Immich, Manyfold (moved
  MIT→AGPL deliberately), MediaManager, Mydia, MediaLyze. Commercial use,
  selling support/hosting, and enterprise self-hosting all remain permitted.
- Dependency compatibility is clean either way (MIT/Apache/BSD/LGPL deps;
  Meilisearch/Postgres run as separate processes — no license coupling).
- Trade-off: some corporations blanket-ban AGPL code even for internal use,
  which can cost adoption/contributors. Agent binaries distributed to end
  machines are equally fine under either license.

**CONFIRMED (2026-07-07): AGPL-3.0-or-later** for the server + agents
(strongest guarantee the project and its forks stay open, matching category
precedent). Already reflected in LICENSE and backend/pyproject.toml. Keep
contributions CLA-free (DCO sign-off instead) so no single party can
relicense, and register the "Filearr" name/logo as the trademark lever.

## 10. Sequencing sketch
v1.x (current roadmap) → **v2**: content extraction + OCR, saved searches,
hash search UI, semantic search, alerting rules, extended metadata profiles,
custom displays → **v3**: agent platform (enrollment CA, local index +
replication, central policy), RBAC + LDAP/SAML/OIDC, machine groups, local
CLI/web, hot-folder scheduling, per-agent alerting.

## 11. Deferred enhancements from T1 (ffprobe video extraction)
- **Precise HDR10+/DoVi profiling.** T1 detects HDR from stream-level colour
  signalling (transfer=smpte2084 → HDR10, arib-std-b67 → HLG, DOVI side-data →
  Dolby Vision, bt2020 primaries → generic HDR). Distinguishing HDR10 vs HDR10+
  reliably, and reading the Dolby Vision profile/level, needs per-frame side
  data (`ffprobe -read_intervals` / `-show_frames`) — a heavier probe. Deferred
  as a major item; revisit with a dedicated "deep probe" opt-in when a real HDR
  library exists to validate against.
- **pymediainfo cross-check.** The stack already pins `pymediainfo`; a second
  extractor could corroborate codecs/track languages and fill fields ffprobe
  omits (e.g. some container-specific tags). Left out of T1 to keep a single
  source of truth; consider a merge strategy later.

## 12. Sidecar follow-ups (deferred from T3, 2026-07-07)
T3 shipped detection + parent linking (`items.sidecar_of`, ondelete CASCADE),
Kodi NFO → parent `metadata` parsing (defusedxml, XXE-safe), and search
exclusion (`is_sidecar` filterable; endpoint hides sidecars unless
`include_sidecars=true` or `sidecar_of=<id>`). Remaining, non-trivial:
- **JRiver `*_JRSidecar.xml` parsing.** T3 detects + links these only. JRiver
  has no ecosystem API and the XML schema is proprietary/verbose; treat as a
  future extracted-metadata source once the field mapping is reverse-engineered
  (parse defensively, same untrusted-input posture as NFO).
- **Subtitle sidecars (`.srt`/`.ass`/`.sub`).** Currently NOT classified as
  sidecars (they are arguably first-class assets users search for). Decision
  needed: link-and-hide vs. keep as searchable items with a `subtitle` facet.
- **Directory-artwork ambiguity.** Directory-level artwork (`poster.jpg`,
  `movie.nfo`) links to the *largest* primary media file in the folder. For
  multi-movie or season folders this may mis-attribute. A stronger model =
  a per-directory "primary item" concept (folder → canonical item) rather than
  the size heuristic; revisit if users report mis-links.
- **NFO as user-facing metadata source.** NFO values currently land under
  `nfo_*` keys in extracted `metadata` and only promote to `title`/`year` when
  empty. A future "metadata source priority" setting (NFO vs. ffprobe vs.
  guessit vs. online scraper) would let users choose authority per field.


## 13. Move/rename detection follow-ups (deferred from T2, 2026-07-07)
T2 shipped identity transfer on rename/move: at scan end, before tombstoning,
vanished rows are matched to newly-discovered rows by `(quick_hash, size)`,
confirmed with `content_hash` when both sides have one, and — only when
unambiguous — the original id/tags/user_metadata/external_ids/first_seen are kept
while path/rel_path/filename/mtime/hashes move onto the surviving row. Ambiguous
buckets (multiple candidates a content hash can't separate) fall back to
tombstone+create; counts land in `ScanRun.stats` (`moved`, `move_ambiguous`).
Deliberately out of scope / open for later:
- **Hashing new files on the scan thread.** `detect_moves` computes quick/content
  hashes for newly-discovered rows synchronously so it can match at scan end. On a
  network mount with a large first-scan or a big new drop this front-loads IO that
  the extract queue would otherwise spread out. When T7 (per-library "quick_hash
  only on network storage" / size-ceiling) lands, move detection should honour the
  same policy and skip full-hash confirmation above the ceiling (matching then
  rests on `(quick_hash, size)` alone — acceptable given the ambiguity guard).
- **In-place content change is not an identity event.** A file whose *bytes*
  change but whose `rel_path` is unchanged is treated as `changed` (identity =
  `(library_id, rel_path)` is stable), never a move. That is correct by the
  identity invariant, but means a "swap in place" (A.mkv and B.mkv exchange
  contents, names unchanged) does not swap identities. No action expected; noted
  so the behaviour is not mistaken for a bug.
- **Cross-library moves.** Detection is scoped to a single library's row set. A
  file relocated from library X to library Y tombstones in X and is created fresh
  in Y (identity not carried). Cross-library identity transfer would need a global
  hash index and a policy for differing include/exclude rules; defer to v2.
- **quick_hash-only ambiguity at scale.** quick_hash is first+last 64 KiB xxh3;
  distinct files that share head+tail+size (e.g. same intro/outro, padded
  containers) collide. Today such a collision during a move is refused
  (`move_ambiguous`) unless a full `content_hash` disambiguates. A future
  mid-file sampling tier could rescue more true moves without a full re-hash.

## 14. SSE live-progress follow-ups (deferred from T4, 2026-07-07)
T4 shipped native `EventSourceResponse` for `GET /scans/{id}/events` (progress /
done / error events, framework keepalive pings, clean disconnect teardown), an
SSE-consuming Admin page (live batch counter + files/s, bounded-backoff
reconnect, one authoritative refresh on stream end), and the AGPL §13 footer
"Source" link (`__SOURCE_URL__`, overridable via `FILEARR_SOURCE_URL` at build
time). Remaining, non-trivial:
- **Push instead of poll (major).** The stream still polls `ScanRun.stats` once
  per second inside the request handler. A true push path — Postgres
  `LISTEN/NOTIFY` fired from the scan task's batch commit, or a Procrastinate
  event — would cut latency to real-time and drop the per-connection DB read
  loop. It touches the scan task's publish mechanism (owned by another agent
  during T4) and needs a NOTIFY payload contract, so it was deferred. The
  current handler is additive-only over `stats`, so a NOTIFY layer can slot in
  without changing the wire schema.
- **Query-param API key for SSE (revisit for tenant tokens).** `EventSource`
  can't set an `Authorization` header, so the events endpoint also accepts the
  key as `?api_key=` (read scope, this endpoint only; verified via the same
  hash+scope path, never logged). Query-string secrets can leak into proxy /
  access logs. When the v2 auth work lands (roadmap items 4/5), replace this
  with a short-lived same-origin cookie or a scoped one-time stream token minted
  by an authenticated POST, and drop the query-param path.
- **Multiplexed progress stream (scalability).** One `EventSource` per running
  scan is fine for a handful of libraries; a single `/scans/events` firehose
  (all running scans over one connection, filtered client-side) would scale
  better for large multi-library deployments. Deferred until it matters.

## T5 — Scheduled + watch-mode scanning (shipped, with follow-ups)

Shipped: one static Procrastinate periodic task `filearr.worker.schedule_scans`
(`@periodic(cron="* * * * *")`) that evaluates each enabled library's
`scan_cron` against the tick with **cronsim** (croniter is EOL) and defers a
scan for the due ones; a scan already `running` for a library is skipped, and a
`queueing_lock` of `scan:<library_id>` collapses a duplicate/late tick (or a
tick racing a manual scan) so a minute can enqueue at most one scan. `scan_cron`
is validated at the API on create/PATCH (invalid → 422; empty/null disables).
Watch mode is watchfiles-based, refused server-side for network roots
(`/proc/self/mountinfo` fstype classification: cifs/nfs/fuse-remote → refused),
debounces change bursts into one normal full scan, and is supervised by a
reconcile loop that starts/stops watchers on config change without a restart.

Follow-ups (deferred):
- **Run the watch supervisor as a first-class process (medium).** The supervisor
  entrypoint `filearr.worker.run_watch_supervisor()` exists but is not yet wired
  into a container. Options: (a) a dedicated `watcher` compose service running
  `python -m filearr.watchd` next to the Procrastinate worker; (b) a Procrastinate
  worker startup hook that launches it as a background task in the same loop.
  Chose to ship the reusable supervisor + entrypoint now and wire the process in
  a follow-up so the periodic scheduler (the primary T5 deliverable) isn't
  blocked on the watcher's deployment shape. Until wired, watch_mode is validated
  and persisted but only *acted on* once the supervisor process runs.
- **Incremental single-file updates on watch events (major).** Today a watch
  event triggers a normal whole-library scan (walk + diff + tombstone). That is
  intentional — move/rename detection and sidecar association only make sense
  with whole-library context, and one scan path is far less risky than two. A
  true incremental path (upsert/extract just the changed paths from the
  watchfiles event set, skipping the full walk) would cut latency and IO on
  large libraries but needs: partial move/sidecar reconciliation, a correct
  tombstone story for deletes seen only via inotify, and careful interaction
  with the batched-commit/cancellation invariant. Deferred as a major item.
- **Sub-minute / second-level cron (low priority).** cronsim supports 6-field
  (seconds) expressions, but the tick is 1-minute (Procrastinate periodic
  granularity), so seconds are ignored. If sub-minute scheduling is ever wanted,
  it needs a faster tick or an in-process timer, not the periodic task.

## 15. Extractor follow-ups (deferred from T6, 2026-07-07)
T6 shipped the remaining per-type property extractors: **model3d** (trimesh —
triangle/vertex counts, bbox extents + volume, watertight flag, multi-mesh scene
aggregation for GLTF/GLB/3MF), **document** (pypdf page count + core properties +
encrypted flag; python-docx core props + paragraph count), **spreadsheet**
(openpyxl read_only/data_only — sheet names/count + core props, no cell load, no
formula evaluation), and **audiobook** m4b chapters (mutagen `chpl`, layered on
top of the existing tinytag tag read). All parsers are size-ceiling-guarded
(`FILEARR_MODEL3D_MAX_BYTES` / `FILEARR_DOCUMENT_MAX_BYTES`, both 256 MiB) and
degrade to `_extract_error` on hostile/corrupt input without failing the job.
Remaining, non-trivial:
- **CAD/proprietary 3D geometry (major).** STEP/STP, FBX, and BLEND get only a
  lightweight `{"unsupported": true}` marker — trimesh has no safe pure-Python
  loader for them, and pulling in `cascadio`/OpenCASCADE (STEP) or the Blender
  Python API (BLEND) is a heavy, native-dependency decision. Defer until there
  is real demand; when it lands, keep the same size ceiling + subprocess
  isolation discipline as ffprobe (never load an untrusted CAD kernel in-process
  without a sandbox).
- **Watertight accuracy vs. cost (medium, deferred).** model3d parses with
  trimesh `process=False` (no vertex-merge/repair) so an untrusted mesh gets no
  expensive processing pass. A side effect: meshes with duplicated vertices
  (e.g. a naive STL export) report `watertight: false` and an inflated vertex
  count even when the surface is closed. An opt-in "accurate geometry" tier
  (`process=True` under a stricter, smaller size ceiling) would fix this for
  users who want it, without exposing the default scan path to the extra cost.
- **Document/e-book text extraction for search (major, v2).** v1 deliberately
  extracts *properties only* — no PDF/DOCX body text, no EPUB/MOBI/CBZ content.
  Full-text indexing (feeding extracted body text into the Meili projection) is
  a v2 feature with its own resource-bounding, language-detection, and
  zip-bomb/decompression-ratio-guard requirements. The current extractors are
  structured so a text pass can be added as a separate, independently-bounded
  stage without changing the property schema.
- **Zip decompression-ratio guard (medium).** docx/xlsx/3mf are ZIP archives;
  today they are bounded only by the *compressed* file-size ceiling. openpyxl
  read_only + no-cell-load and python-docx's structure-only reads keep memory
  modest in practice, but a belt-and-suspenders decompressed-size / entry-count
  guard (reject archives whose declared uncompressed size exceeds a ratio
  threshold before handing them to the parser) would harden against a crafted
  zip bomb. Deferred as medium — the size ceiling covers the common case.

## 16. Hash-policy follow-ups (deferred from T7, 2026-07-07)
T7 shipped per-library hash policy: `hash_policy` (`auto` | `full` |
`quick_only`) + a nullable `hash_full_max_bytes` per-library ceiling override
(null → global `FILEARR_SCAN_HASH_FULL_MAX_BYTES`). `auto` detects the root's
filesystem via T5's `is_network_path` (network → `quick_only` behaviour, local →
`full`), resolved ONCE per scan run and recorded in `ScanRun.stats.hash_policy`
for observability. quick_hash is always computed; only the whole-file
`content_hash` stream is gated. Move detection (T2) honours the resolved
`compute_content` flag: under `quick_only` a `(quick_hash, size)` collision that
would need `content_hash` to disambiguate stays ambiguous and is refused
(counted `move_ambiguous`) — integrity is never traded for a blind transfer.
Remaining, non-trivial:
- **RESEARCH PHASE REQUIRED (user-reported 2026-07-16): quick_hash produces
  thousands of false duplicate detections on SMALL files.** The duplicates
  surface (P3-T10 badge + the `duplicate_files` canned report) falls back to
  `(quick_hash, size)` grouping when `content_hash` is absent (`quick_only` /
  network libraries), and live data shows large clusters of small files
  flagged as duplicates that are NOT byte-identical. Research must (a)
  reproduce and root-cause the collisions — quick_hash samples first+last
  64KiB via xxh3, so for files ≤128KiB it covers the whole file and identical
  hash+size *should* imply identical bytes: establish whether the false
  positives come from the sampling window (files >128KiB with identical
  head/tail, e.g. padded/templated formats), from zero/sparse regions, from a
  hashing bug (offset/length handling on short files), or from the grouping
  logic itself (e.g. size not actually constrained within a group); (b) design
  the size-floor logic — do not use sampled hashing below a threshold where
  it adds nothing (small files should get a cheap FULL-file hash instead:
  full xxh3/xxh128 of a ≤128KiB file is faster than two seeks), and pick the
  threshold from data; (c) benchmark more reliable full-file hash candidates
  for the replacement tier (xxh3-128 full-file, BLAKE3, SHA-256 for the
  crypto-needed paths) on representative corpus sizes over local disk AND SMB
  — document throughput/collision trade-offs and a recommendation. Output: a
  `docs/research/` brief with the reproduction, the chosen size floor, the
  benchmark table, and the migration story for already-stored quick_hash
  values (hash_policy_version bump + lazy re-hash per FIX/cfg1 provenance
  machinery). Duplicate-report UX must state which hash tier grouped each
  cluster until the fix lands.
  A `quick_only` library never stores `content_hash`, so exact-duplicate
  detection and cross-library dedupe are weaker there. A future opt-in,
  low-priority background task could stream full hashes for network items during
  idle windows (rate-limited, cancellable, respecting the same ceiling) so the
  integrity benefit of content hashes is available without paying the cost on
  the hot scan path. Deferred until dedupe/versioning (roadmap) actually needs
  it.
- **Per-file `auto` re-detection in the extract worker (minor).** The scan
  resolves `auto` once (a mountinfo parse) and stashes the result in
  `ScanRun.stats`; the extract worker, running in a separate process, currently
  re-resolves from the library row per item (one mountinfo parse per file). This
  is correct but slightly redundant; a short-TTL per-(library, mount) cache of
  the network classification would remove the repeat parse. Minor — mountinfo
  parsing is cheap and bounded — so left as a future optimisation rather than
  threading a resolved-policy token across the job queue.

## 17. Extraction throughput (T8 follow-ups)
- **Adaptive extract concurrency / backpressure (major, deferred).** T8 ships
  *static* knobs: per-worker `--concurrency`, per-queue worker pinning, and a
  negative extract priority so scan-control jumps the queue. It does **not**
  auto-tune. A v2 controller could watch the `extract` queue depth (already
  exposed in `/api/stats`) and the DB/CPU pressure and scale worker concurrency
  or throttle defer rate dynamically (token-bucket on the defer path, or a
  Procrastinate worker `--shutdown-graceful-timeout`-aware autoscaler). Deferred
  as major: it needs a control loop, metrics history, and careful anti-thrash
  hysteresis; the static knobs cover the "don't starve the API during a big
  scan" acceptance criterion today. When built, keep queue depth as the primary
  signal and never let extraction preempt scan-control (the priority invariant).
- **Per-library / per-run throughput history (medium, deferred).** `files_per_s`
  and `walk_seconds` are recorded on each `ScanRun.stats` but not aggregated. A
  small rollup (rolling median files/s per library, extract-drain time) would let
  the Admin UI show "this scan is slower than usual" and size worker counts from
  real history rather than a guessed default. Cheap to add on top of the existing
  per-run stats; deferred only for UI scope.

## 18. Error surfacing (T11 follow-ups)
- **Persisted job error text / tracebacks (major, deferred).** Procrastinate
  3.9's `procrastinate_events` table stores only `(job_id, type, at)` — it does
  **not** persist the exception message or traceback of a failed job (that goes
  to worker logs). So `/api/system/failed-jobs` can surface *which* job failed,
  its queue/task, attempt count, and *when* the last event fired, but `error` is
  always null. Capturing the actual traceback would need a custom failure hook
  (Procrastinate `JobContext`/retry callback) writing to a dedicated
  `job_errors` table (job_id, attempt, sanitized message, ts) with its own
  retention purge. Deferred as major: it adds a write path + table + purge and
  duplicates what structured worker logs already give operators. The item-level
  `_extract_error` path (parser messages, surfaced per-library) already covers
  the "corrupt file is visible, not silent" acceptance criterion; the missing
  piece is only *infra* job failures (OOM, DB blips), which logs capture today.
- **Per-run error counter is best-effort, not authoritative (design note).**
  The atomic `ScanRun.stats.extract_errors` counter is bumped by the extract
  worker via a single race-free SQL `jsonb_set` increment, but extract jobs run
  asynchronously *after* the scan finishes and a run row can be purged/absent, so
  the counter can undercount (never over). The **authoritative** count is the
  live GIN-indexed aggregate (`items.metadata ? '_extract_error'`, exposed in
  `/api/stats.extract_errors` and `/api/libraries/{id}/errors`). If a strictly
  exact per-run attribution is ever required, it needs a transactional outbox
  linking item→run at extract time (major); the current split (authoritative
  live count + convenience per-run counter) is the confirmed T11 approach.

## 19. Test suite + CI (T10 follow-ups)
- **Empty-but-mounted root vs dead mount (deferred; infra-owned).** The T10
  `scan.assert_scannable_root` pre-flight aborts a scan when the root is missing,
  not a directory, or `scandir` raises (ENOENT/ENOTCONN/EACCES) — this stops a
  dropped mount that *disappears* or *errors* from tombstoning the whole library
  (invariant 7). It intentionally does NOT abort on an **empty but readable**
  directory: that is indistinguishable from a legitimately-emptied library, and
  refusing to scan it would break real "user deleted everything" cases. A dead
  FUSE bind that presents as a *stale-empty* readable mountpoint is therefore
  still handled at the infra layer (compose `bind.propagation: rslave` + the
  deploy-time read-test), not in code. A future code-level guard could compare
  the walk's seen-count against the prior scan and refuse a scan that drops from
  N>0 to 0 files unless a `--force-empty` / library flag is set (heuristic;
  needs a false-positive escape hatch). Deferred: risk of blocking legitimate
  bulk deletions outweighs the marginal gain over the infra fix.
- **CI matrix / caching polish (minor).** The gate runs a single Python (3.13)
  and single Node (24) to mirror production; a version matrix (e.g. Python 3.13
  only for now, 3.14 once C-ext wheels land per the Dockerfile note) can be added
  when 3.14 support is in scope. uv + npm caches are enabled; a pytest-xdist
  parallel split and a Meili-backed projection integration job (currently the
  index sync is unit-tested with the defer mocked) are candidates as the suite
  grows past Phase 1.
- **Ruff scoped ignores are documented, not silent (design note).** CI is a true
  lint gate (`ruff check .` must pass). Pre-existing idioms are accepted via
  documented per-file/global ignores rather than code churn: `B008` (FastAPI
  `Depends()` default), `UP042` (`class X(str, enum.Enum)` — load-bearing for
  SQLAlchemy Enum + JSON/Meili serialization; migrating to `enum.StrEnum` changes
  `str(member)` semantics), init_db `E402` (deliberate `sys.path` shim), and
  `alembic/versions` excluded (autogenerated). Revisit `UP042` only alongside a
  deliberate StrEnum migration with serialization tests.
