# Runbook — scan scheduling (FIX-8)

## Re-enable schedules after the scan-storm fix
The two library schedules were disabled as interim mitigation for the
scan-scheduling storm (see `docs/fixes/FIX-8-scan-scheduling-storm.md`). After
the fix is deployed (migration `a4c8e1f6b2d9` applied) and any stuck `doing`
jobs / `running` ScanRuns are cleaned (SQL in the fix doc, or wait ~5 min for the
reaper), re-enable them from **Admin → each library → Scan schedule**, or via the
API (`PATCH /api/v1/libraries/{id}` with `scan_cron`).

Firing is now **once per cron occurrence** (`libraries.last_cron_fired_at`): a
missed occurrence produces a single catch-up scan (never a per-tick storm), and a
scan is skipped while any unfinished `scan_library` job for that library exists.

## Verifying it is healthy
- `GET /api/v1/system/jobs` (Jobs page): at most ONE `scan_library` per library in
  todo/doing at a time.
- A worker death mid-scan now self-heals: the reaper (every 5 min) fails the
  orphaned job AND flips its `ScanRun` `running → failed`, unblocking the next
  scheduled scan.
- Runaway `attempts` no longer accumulate: stalled non-scan jobs are requeued at
  most `FILEARR_REAP_MAX_ATTEMPTS` (default 10) times, then failed.

## Tunables (env)
- `FILEARR_SCAN_SCHEDULE_MAX_CATCHUP_MINUTES` (default 2880 / 48h) — furthest-back
  missed occurrence a recovery tick will fire.
- `FILEARR_REAP_MAX_ATTEMPTS` (default 10) — reaper requeue budget for non-scan
  jobs before they are failed.
- `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` (default 600 / 10m) — how long a
  `running`/`stopping` ScanRun with no live scan job may sit before the reaper
  reconciles it terminal (FIX-15).
- `FILEARR_SOURCE_URL` — AGPL §13 source link shown in the UI footer (also served
  by `GET /api/v1/version`).

## Stuck `stopping` / `running` ScanRuns (FIX-15)
A ScanRun can wedge non-terminal if a graceful **Stop** was requested (or a scan
crashed) and the scan job then left the `doing` state (succeeded / failed /
cancelled / purged) before any live worker or the stalled-job reaper transitioned
its row. Such a row is invisible to the reaper (which only acts on stalled *doing*
jobs) and BLOCKS the scheduler's running-row guard (`status IN ('running',
'stopping')`) for that library/scope forever.

- **Auto-heal:** the every-5-min maintenance reaper now also runs a reconciler —
  any `running`/`stopping` ScanRun older than
  `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` (default 600) with NO scan_library
  job in todo/doing/aborting for its (library, scope) is driven terminal:
  `stopping → stopped` (honors the stop intent), `running → failed` (invariant 7).
  Counted in the reaper result as `scan_runs_reconciled`.
- **Manual:** `POST /api/v1/scans/{id}/force-clear` (admin, audited in
  `security_events` as `scan_force_cleared`) forces any non-terminal run to
  `stopped`; it refuses (409 "still active") only when a live worker is genuinely
  draining it — use `/stop` there. The Admin page shows a **Force clear** button
  next to a run stuck in `stopping`.
- **Stop hardening:** `/stop` now finalizes directly to `stopped` when no live
  worker is draining the run (instead of leaving a `stopping` marker that would
  never be observed); it keeps the graceful path when a worker IS alive.
- Note: a **manual** library scan (`POST /libraries/{id}/scan`) already self-heals
  these rows (it flips leftover running/stopping → failed before deferring); the
  reconciler + force-clear fix the **scheduled/watch** path and the UI.

## If OOM recurs
Concurrent full scans over SMB each hold a whole-library map in RAM. Stagger
library `scan_cron` times so two large libraries do not scan simultaneously,
and/or raise the worker container memory limit. See the OOM linkage section in
the fix doc.
