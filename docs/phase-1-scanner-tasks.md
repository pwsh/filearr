# Phase 1 — Scanner & Extraction Hardening

Informed by the 2026-07-06 live deployment (Proxmox LXC, SMB via rclone FUSE).
**Measured baseline:** walk+insert ≈ 24 files/s over SMB (1,190 files / 50 s);
extraction ≈ 20 ms/file; index sync keeps pace (Meili 202s, no queue backlog).

## Defects found live (fixed, need regression tests)
- [x] Single-transaction scan → batched commits every 250 files with progress publishing
- [x] Orphaned "running" scan rows after worker restart → reap on trigger + crash handler
- [x] No scan cancellation → cancel endpoint + between-batch abort check
- [x] Extract jobs deferred before batch commit (race) → defer-after-commit
- [x] Items committed but never extracted → self-heal re-queue when quick_hash is null
- [x] Dead FUSE mount kills container view → compose rslave propagation + deploy read-test
- [x] Regression tests for all of the above (T10, `test_scan_defects_t10.py`)
- [x] Extractor hotfix batch (live 750k-item deploy, 2026-07-09): unvalidated
      tag/parse data reaching typed columns. (A) `int('2007-10-09')` on audio
      date-string years and (B) year-as-list (guessit multi-year / multi-value
      tags) killing the extract JOB at `session.commit` (psycopg CannotCoerce)
      → `coerce_year`/`coerce_str` at every typed-column assignment + a belt-
      and-braces commit that downgrades a DB-layer failure to `_extract_error`.
      (C) `.ape`/WavPack/Musepack "No tag reader found" → tinytag→mutagen
      fallback. (D) pypdf "Can not convert date" → per-field defensive PDF
      metadata access (keep raw string). Plus a `POST /libraries/{id}/
      retry-extracts` admin action + UI button. Regressions in
      `test_extract_hotfix.py`.
- [x] Missing/unreadable scan root now ABORTS the scan (`ScanRootError`) before
      the diff phase instead of tombstoning the whole library on a dead mount
      (`scan.assert_scannable_root`; invariant 7). Bug was live: an *empty*
      readable dir still tombstones (legit empty library) — that residual is
      the rslave/infra fix's job, documented in future-roadmap.

## Tasks

### T1 — ffprobe video extraction
Full technical metadata: container, video/audio codecs, resolution, HDR flags,
duration, bitrate, audio/subtitle track list. `subprocess` + `-print_format json`,
timeout guard, merge with existing guessit title/year/episode parse.
*Accept:* Arcane episode shows codec/resolution/duration in item detail; corrupt
file writes `_extract_error` without failing the job.

### T2 — Move/rename detection before tombstoning  [DONE 2026-07-07]
At scan end, match candidate-tombstones against new items by (quick_hash, size);
confirm with full hash when both exist. Transfer identity (id, tags, user_metadata,
external_ids) instead of tombstone+create.
*Accept:* renaming a file between scans preserves item id and edits.
**Done:** `filearr/tasks/move.py` (`plan_moves` pure planner + `detect_moves`
transfer) runs in `scan._scan_body` before tombstoning and before the T3
association pass. Integrity-first: ambiguous (quick_hash,size) buckets that
content_hash cannot split are refused and fall back to tombstone+create
(`stats.move_ambiguous`); confirmed moves increment `stats.moved`. Transfer uses a
delete-duplicate -> park-survivor-at-sentinel -> final-rel_path sequence so the
unique (library_id, rel_path) index is never transiently violated. Sidecars carry
no hash, so their old rows tombstone and the association pass re-links the fresh
rows to the surviving parent id. Deferred items (network hashing cost, cross-
library moves, in-place content swap) recorded in future-roadmap.md section 13.

### T3 — Sidecar association
Detect `.nfo`, `poster.jpg`, `-thumb.jpg`, `*_JRSidecar.xml` etc.; link to parent
media item (`sidecar_of` FK or metadata ref) and exclude from default search
(filterable, not gone). Parse Kodi NFO into extracted metadata when present.
JRiver sidecar XML = future metadata source (JRiver has no ecosystem API).
*Accept:* Arcane episode search no longer surfaces its nfo/thumb as separate
top-level "other" hits; NFO title/plot lands in item metadata.

### T4 — SSE live progress + admin polish
Switch `/scans/{id}/events` to native `fastapi.sse.EventSourceResponse`; Admin page
consumes SSE instead of 3 s polling; show batch counter + files/s rate.
*Accept:* progress visibly ticks in UI during a scan without refresh.

### T5 — Scheduled + watch-mode scanning
**Decided (2026-07-06):** Procrastinate periodic tasks are import-time static,
so per-library cron uses a single static tick task — `@periodic(cron="* * * * *")`
— that reads enabled libraries, evaluates each `scan_cron` against the tick
timestamp with **cronsim** (croniter is EOL), and defers scans that are due,
skipping libraries with a scan already running. No worker restarts on library
edits; 1-minute granularity. Optional watchfiles-based watch mode with a hard
"local paths only" guard (inotify unreliable over SMB/NFS — confirmed by
research and by our own FUSE mount behaviour).
*Accept:* cron expression on a library fires unattended scans; watch mode refuses
to enable on /data/media (network) paths.

**Done (2026-07-07):** `filearr.worker.schedule_scans` periodic tick + cronsim
(`filearr.schedule.cron_is_due` / `validate_cron`), running-scan skip +
`scan:<id>` queueing-lock idempotency; API validates `scan_cron` and refuses
`watch_mode` on network roots (422) via `/proc/self/mountinfo` fstype detection
(`filearr.schedule.is_network_path`); `LibraryUpdate` PATCH endpoint;
watchfiles supervisor (`filearr.watch.WatchSupervisor`, entrypoint
`filearr.watchd`) with debounce → full scan and restart-free reconcile. Watcher
*process* wiring + incremental single-file updates deferred to future-roadmap.

### T6 — Remaining extractors  ✅ (2026-07-07)
model3d via trimesh (tri/vertex count, bounds, watertight), document/spreadsheet
properties (pypdf/python-docx/openpyxl, metadata only), m4b chapters via mutagen.
*Accept:* stats.by_type shows populated metadata for all enabled types.
*Done 2026-07-07:* new modules `tasks/model3d.py`, `tasks/documents.py`,
`tasks/audiobook.py`; `extract.py` dispatch wired (audiobook = tinytag tags +
mutagen chapters, model3d/document = size-guarded, `_extract_error` on failure).
Size ceilings `FILEARR_MODEL3D_MAX_BYTES` / `FILEARR_DOCUMENT_MAX_BYTES` (256 MiB
each). STEP/FBX/BLEND + non-office docs → `{"unsupported": true}` marker (no
geometry/props loader; not an error). CAD geometry, doc text extraction, opt-in
accurate-watertight tier, and a zip decompression-ratio guard deferred to
future-roadmap §15. Tests: 16 unit + 5 DB round-trip, full suite green, ruff clean.

### T7 — Hash policy for network mounts  [DONE 2026-07-07]
Per-library toggle: skip full-hash on network storages (quick_hash only), or
size ceiling override. 40 GB of video over SMB is the pain point.
*Accept:* library setting visibly changes scan IO profile.

*Done 2026-07-07:* `libraries.hash_policy` (`auto` | `full` | `quick_only`,
default `auto`) + nullable `hash_full_max_bytes` override (migration rev
`b7d2e4f6a891`). Policy resolved ONCE per scan run in `filearr.hashpolicy.
resolve_hash_policy` (reusing T5's `is_network_path` for `auto`: network ->
`quick_only`, local -> `full`) and recorded in `ScanRun.stats.hash_policy`; the
per-file extract worker re-resolves from the library row via the same helper.
quick_hash is always computed -- only the whole-file `content_hash` stream is
gated (by policy AND the per-library-or-global byte ceiling). T2 move detection
takes a `compute_content` flag so `quick_only` libraries refuse an
un-content-confirmable `(quick_hash, size)` collision (counted `move_ambiguous`)
rather than transferring identity blind -- integrity preserved. API validates the
two fields (positive ceiling, enum policy) on create/PATCH; AdminPage exposes a
policy dropdown + ceiling input per library and in the create form. Follow-ups
(content-hash backfill; extract-worker detection cache) in future-roadmap.md #16.

### T8 — Extraction throughput controls  ✅ done 2026-07-07
Procrastinate queue concurrency tuning, batch-defer for extract jobs, per-queue
worker counts in compose (worker replicas), scan-rate metrics in stats.
*Accept:* documented knobs; 5k-file fixture scan doesn't starve API responsiveness.

**Delivered:**
- **Queues (already separated) + priority.** Jobs run on `scan` / `extract` /
  `index` / `maintenance`. Extract jobs are now deferred at a **negative
  priority** (`FILEARR_EXTRACT_PRIORITY`, default `-10`; higher int = runs first
  in Procrastinate, scan-control defaults to 0) so a freshly-triggered scan or
  cancel is never starved behind a 5k-file extract backlog on a shared worker.
  Queue names are settings (`FILEARR_QUEUE_{SCAN,EXTRACT,INDEX,MAINTENANCE}`).
- **Batch defer.** `scan._defer_extract` (one `defer_async` per file) became
  `_defer_extract_batch`, using Procrastinate 3.9 `JobDeferrer.batch_defer_async`
  (one multi-row INSERT for the whole batch). The **defer-AFTER-commit** contract
  is unchanged — it is still only called once each 250-file batch has committed
  (and once at scan end); an empty batch is a no-op.
- **Compose knobs.** `worker` service reads `FILEARR_WORKER_CONCURRENCY`
  (default 4) and `FILEARR_WORKER_QUEUES` (default = all) via an `sh -c` wrapper
  that maps them onto `procrastinate worker --concurrency/--queues`. Scale-out is
  documented inline: `docker compose up -d --scale worker=N` (all workers serve
  all queues; extract's negative priority protects scan control), or a dedicated
  `extract-worker` pinned to `--queues extract` with the primary worker set to
  `FILEARR_WORKER_QUEUES=scan,index,maintenance`.
- **Stats.** `GET /api/stats` gained `queues` (per-queue todo/doing/succeeded/
  failed rollup) and a flat `extract` summary (`depth` = backlog, `running`,
  `done`, `failed`) from a single aggregate read over `procrastinate_jobs`
  (`filearr/queue_stats.py`; empty + non-raising if the queue schema is absent).
  `ScanRun.stats` gained `files_per_s` and `walk_seconds` (monotonic-clock walk
  throughput; extract runs out-of-band and is tracked by the queue-depth stat).
- **Tests:** `tests/test_throughput_t8.py` — real Procrastinate schema on
  pgserver: batch defer lands N jobs on `extract` at negative priority, empty
  batch is a no-op, commit-before-defer ordering, queue-depth stat correctness
  (+ empty-schema path), env parsing, and a 500-file end-to-end scan smoke that
  asserts one extract job per new file + `files_per_s` recorded. 117 tests green.

### T9 — Alembic baseline
Replace create_all bootstrap with a real initial migration; init_db applies
migrations; document upgrade flow.
*Accept:* fresh deploy and existing DB both reach same schema via alembic.

### T10 — Test suite + CI
Fixture tree (all media types incl. sidecars), pytest for walk/diff/tombstone/
move/cancel/self-heal, extraction unit tests with tiny real files, GitHub Actions
running lint+tests before image build.
*Accept:* CI green gate; the six live-found defects each have a regression test.

**Done (2026-07-07).** Reusable `media_tree` fixture (conftest) generates every
enabled MediaType + sidecars (video+nfo+thumb+dir-poster) + junk at test time
(ffmpeg + pure-python libs; no committed binaries). E2E `test_scan_e2e_t10.py`
scans+extracts it and asserts by_type populated for all types, sidecars linked
(`sidecar_of`) and hidden (`is_sidecar`), NFO folded into parent, junk ignored.
`test_scan_defects_t10.py` = one named regression per live defect: batched
commits + mid-scan progress publish, crash→failed (never `running`) + reap on
trigger, cancel endpoint + between-batch abort, real-ordering defer-after-commit,
self-heal re-queue on null quick_hash (+ skip-if-hashed), and the missing/
unreadable-root guard (no library-wide tombstone). Product change (minimal):
`scan.assert_scannable_root` pre-flight, first statement of `_scan_body`.
CI `.github/workflows/ci.yml`: backend (uv sync, ruff, pytest; apt ffmpeg;
pgserver self-bootstraps PG — no service container) + frontend (npm ci,
svelte-check, vite build) + alembic-check (postgres:18 service, upgrade→`alembic
check` for model/migration drift), all gating a no-push docker build. Ruff made
clean project-wide via scoped, documented ignores (B008 FastAPI idiom, UP042
str-Enum by design, init_db E402 sys.path shim, alembic/versions excluded) and
one real fix: ASYNC240 removed by having `walk()` yield `rel` (no more
`os.path.relpath` in the async body). `_psycopg3` helper consolidated into
conftest (`psycopg3_uri`). 167 tests green (154 + 13).

### T11 — Error surfacing  ✅ (2026-07-07)
`_extract_error` count per scan in stats + Admin badge; failed-jobs view (read
Procrastinate tables); scan row keeps last error message.
*Accept:* deliberately corrupt file shows up as a visible error count, not silence.

**Done.** New `filearr/errors.py`: `sanitize_error` (strip C0/C1 control chars,
cap 500) + cheap GIN-indexed aggregates (`extract_error_count`,
`extract_error_counts_by_library`, paginated `failing_items`) + `failed_jobs`
(read-only over `procrastinate_jobs`, capped 100). Attribution = **both**: the
authoritative live library-wide count (`metadata ? '_extract_error'` over the
existing `ix_items_metadata` GIN index, in `/stats` + `/libraries/{id}/errors`)
AND a best-effort race-free per-run counter (`ScanRun.stats.extract_errors`,
atomic SQL `jsonb_set` increment; extract jobs now carry `scan_run_id`). Extract
error strings are sanitized at *store* time. Scan crash handler retains
sanitized `stats.error`; the SSE stream emits a dedicated `error` event for a
failed scan. Admin UI: per-library red error badge (expand → failing items) +
Failed-jobs table. Also fixed the T7 PATCH null-clear bug (build the patch set
from `model_fields_set`, not `exclude_unset`, so explicit
`{"hash_full_max_bytes": null}` clears the column). 11 new tests in
`test_errors_t11.py`; full suite 154 green. Deferred (roadmap §18): persisted
job tracebacks (procrastinate 3.9 stores none).

## Explicit non-goals (later phases)
Search UI virtualization at 100k+ (Phase 2), metadata PATCH UI polish (Phase 3),
exports (Phase 4), identification + actions (Phase 5), tag/NFO write-back and
UI-managed remote storage via rclone/fsspec (v2).
