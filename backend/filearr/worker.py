"""Procrastinate app (Postgres-native job queue — no Redis).

Run worker:  procrastinate --app=filearr.worker.proc_app worker
Queues: scan (walk/diff), extract (per-file metadata), index (Meili sync), maintenance.
"""

from datetime import UTC, datetime, timedelta

import procrastinate
from procrastinate import PsycopgConnector
from procrastinate.exceptions import AlreadyEnqueued, UniqueViolation
from procrastinate.jobs import Status

from filearr.config import get_settings
from filearr.db import SessionLocal as SessionLocal  # noqa: PLC0414 (re-export for periodics)

proc_app = procrastinate.App(
    connector=PsycopgConnector(conninfo=get_settings().procrastinate_dsn),
    import_paths=[
        "filearr.tasks.scan",
        "filearr.tasks.extract",
        "filearr.tasks.index_sync",
        "filearr.tasks.alerts",
        # P3-T8 local embedding pipeline (embed queue, lowest priority). Inert
        # until FILEARR_SEMANTIC_ENABLED=true.
        "filearr.tasks.embed",
        # S12/P12 thumbnails: the thumbs-queue ride-along job + the daily orphan
        # GC periodic. Inert until FILEARR_THUMBS_ENABLED (default true).
        "filearr.tasks.thumbs",
        # FIX-11: 5-minutely low-space monitor + emergency thumbnail GC. Inert
        # (returns early) when FILEARR_DISK_MONITOR_ENABLED=false.
        "filearr.tasks.diskmon",
        # P8-T11: 5-minutely agent-offline + replication-stall ops monitor. Inert
        # (returns early) when FILEARR_AGENTS_ENABLED=false or tables absent.
        "filearr.tasks.agentmon",
        # P11-T5/T9: background report-export task (dedicated `exports` queue) +
        # scheduled-delivery evaluation. Inert until a schedule/export exists.
        "filearr.tasks.reports",
    ],
)


def log_startup_disk_status() -> str:
    """Log the current disk status for every watch path at worker startup and
    return the worst status (FIX-11).

    Called once when the worker process boots (from the worker entrypoint / the
    diskmon import). If the worst status is ``critical`` the operator is told the
    thumbnail producer is effectively PAUSED: at critical the fail-closed guard
    (``diskguard.guard_write``) refuses every thumbnail write, so queued
    ``thumb_item`` jobs fail fast with the ``disk_full_guard`` token and write NO
    bytes — the workers stay alive and other queues keep running. Never raises."""
    import logging as _logging

    from filearr import diskguard as _dg
    from filearr.config import get_settings as _gs

    logger = _logging.getLogger("filearr.diskmon")
    try:
        statuses = _dg.monitored_statuses(_gs())
        worst = _dg.overall_status(statuses)
        for st in statuses:
            logger.info(
                "startup disk %s: %s free=%d (%.1f%%)",
                st["status"], st["path"], st["free"], st["pct_free"],
            )
        if worst == _dg.CRITICAL:
            logger.warning(
                "startup disk CRITICAL — thumbnail generation is fail-closed "
                "(guarded writes refused); free space before resuming thumbnails."
            )
        return worst
    except Exception:  # noqa: BLE001 - a monitoring log must never break startup
        logger.debug("startup disk status check failed", exc_info=True)
        return _dg.OK


# --- FIX-6: stalled-job reaper ---------------------------------------------
# Fully-qualified name of the long-running library-scan task. It is EXEMPT from
# the absolute-age net (a full walk of a large library legitimately runs long)
# and, when it IS reaped via the heartbeat net (its worker died), it is FAILED
# rather than retried: the ScanRun row is already crash-failed (invariant 7) and
# an operator retriggers a scan from the Libraries page — silently re-running a
# half-finished scan would be surprising.
SCAN_TASK_NAME = "filearr.tasks.scan.scan_library"

# Detect doing jobs that are stalled. TWO independent nets ORed together:
#   * heartbeat net (ALL doing jobs): worker_id IS NULL (procrastinate SET NULL
#     when the worker row was pruned) OR the job's worker has not heartbeat
#     within :hb seconds. This mirrors JobManager.get_stalled_jobs' own
#     `select_stalled_jobs_by_heartbeat` predicate exactly.
#   * age net (NON-scan doing jobs only): the job's most recent 'started' event
#     is older than :age seconds. scan_library is excluded from this net.
# Runs over the SAME connector procrastinate uses (so tests that rebind the
# app connector see it), returns (id, task) for every stalled job.
_DETECT_STALLED_SQL = """
WITH stalled_workers AS (
    SELECT id FROM procrastinate_workers
    WHERE last_heartbeat < NOW() - make_interval(secs => %(hb)s)
),
started AS (
    SELECT job_id, max(at) AS started_at
    FROM procrastinate_events
    WHERE type = 'started'
    GROUP BY job_id
)
SELECT j.id AS id, j.task_name AS task, j.args AS args, j.attempts AS attempts
FROM procrastinate_jobs j
LEFT JOIN stalled_workers sw ON sw.id = j.worker_id
LEFT JOIN started s ON s.job_id = j.id
WHERE j.status = 'doing'
  AND (
    (j.worker_id IS NULL OR sw.id IS NOT NULL)
    OR (
      j.task_name <> %(scan_task)s
      AND s.started_at IS NOT NULL
      AND s.started_at < NOW() - make_interval(secs => %(age)s)
    )
  )
ORDER BY j.id
"""


async def _fail_scanrun_for_reaped_scan(args: dict | None) -> None:
    """Mark the running/stopping ScanRun(s) of a reaped scan_library orphan
    ``failed`` (invariant 7: a crashed scan must never be left ``running``).

    The live storm's worker died via OOM, so ``scan_library``'s own crash handler
    never ran and the ScanRun row was left ``running`` — blocking every future
    scheduler tick for that library. Because ``procrastinate_dsn`` and
    ``database_url`` address the SAME Postgres in every real deployment, we run
    this UPDATE over the procrastinate connector already in hand (no second
    engine), guarded by ``to_regclass`` so it is a safe no-op on a bare/queue-only
    DB (unit tests). Best-effort + idempotent: a second reaper pass (or a reaper
    whose own instance stalled) simply matches zero rows. Never raises into the
    reap loop."""
    if not args:
        return
    library_id = args.get("library_id")
    if not library_id:
        return
    rel_path = args.get("rel_path")  # None => full-library scan
    connector = proc_app.job_manager.connector
    try:
        reg = await connector.execute_query_one_async(
            "SELECT to_regclass('scan_runs') AS r"
        )
        if reg["r"] is None:
            return
        await connector.execute_query_async(
            """
            UPDATE scan_runs
            SET status = 'failed',
                finished_at = NOW(),
                stats = coalesce(stats, '{}'::jsonb)
                        || jsonb_build_object(
                             'error', 'scan worker died; reaped by stalled-job reaper',
                             'reaped', true
                           )
            WHERE library_id = %(library_id)s::uuid
              AND status IN ('running', 'stopping')
              AND (
                    (%(rel_path)s::text IS NULL AND rel_path IS NULL)
                 OR rel_path = %(rel_path)s::text
                  )
            """,
            library_id=str(library_id),
            rel_path=rel_path,
        )
    except Exception:  # noqa: BLE001 - reaper must never fail on the ScanRun net
        pass


# --- FIX-15: orphaned/stuck ScanRun reconciler ------------------------------
# Drive a non-terminal ScanRun terminal when its scan job is already GONE. The
# graceful-stop transition ('stopping' -> 'stopped') only ever runs inside a LIVE
# scan worker's between-batch check, and the stalled-job reaper only transitions
# a running/stopping ScanRun when it detects a *stalled 'doing' scan job* that
# same tick -- so a 'stopping' (or orphaned 'running') ScanRun whose job left
# 'doing' (succeeded / failed / cancelled / aborted / purged from job history)
# has no stalled job to reap and never converges. 'stopping' honors the operator
# intent -> 'stopped'; orphaned 'running' -> 'failed' (invariant 7). One bounded,
# idempotent UPDATE guarded by a started_at grace window (protects a scan whose
# job row is momentarily not yet visible right after enqueue).
_RECONCILE_SCAN_SQL = """
UPDATE scan_runs sr
SET status = CASE WHEN sr.status = 'stopping' THEN 'stopped' ELSE 'failed' END,
    finished_at = NOW(),
    stats = coalesce(sr.stats, '{}'::jsonb) || jsonb_build_object(
        'reconciled', true,
        'reaped', true,
        'reconcile_note', CASE WHEN sr.status = 'stopping'
             THEN 'stop requested but no live scan job remained; finalized as '
                  'stopped by the maintenance reconciler (FIX-15)'
             ELSE 'orphaned running scan with no live job; failed by the '
                  'maintenance reconciler (invariant 7, FIX-15)'
        END
    )
WHERE sr.status IN ('running', 'stopping')
  AND sr.started_at < NOW() - make_interval(secs => %(grace)s)
  AND NOT EXISTS (
        SELECT 1 FROM procrastinate_jobs j
        WHERE j.task_name = %(scan_task)s
          AND j.status IN ('todo', 'doing', 'aborting')
          AND j.args->>'library_id' = sr.library_id::text
          AND (
                (sr.rel_path IS NULL AND (j.args->>'rel_path') IS NULL)
             OR (j.args->>'rel_path') = sr.rel_path
              )
  )
RETURNING sr.id AS id, sr.status AS status
"""


async def reconcile_orphaned_scan_runs_now() -> dict:
    """Finalize non-terminal ScanRuns whose scan job is GONE (FIX-15).

    Any ScanRun in ``running``/``stopping`` older than
    ``scan_run_reconcile_grace_seconds`` with NO scan_library job in
    {todo, doing, aborting} for its (library, scope) is driven terminal in ONE
    bounded, idempotent UPDATE: ``stopping`` -> ``stopped`` (honor the operator's
    stop intent) and ``running`` -> ``failed`` (invariant 7). This is the net for
    the convergence gap the stalled-job reaper cannot cover -- a run whose job has
    left ``doing`` (succeeded / failed / cancelled / aborted / purged) is invisible
    to the reaper's stalled-``doing`` detection, so nothing else ever revisits it,
    and it blocks the scheduler's running-row guard for that library forever.

    Runs over the SAME connector procrastinate uses (``procrastinate_dsn`` and
    ``database_url`` address the same Postgres in every real deployment), guarded
    by ``to_regclass`` so it is a safe no-op on a bare/queue-only DB. Idempotent
    (a second pass finds the rows already terminal). Never raises into the tick.

    Returns ``{reconciled, stopped, failed}``.
    """
    connector = proc_app.job_manager.connector
    try:
        reg = await connector.execute_query_one_async(
            "SELECT to_regclass('scan_runs') AS sr, "
            "to_regclass('procrastinate_jobs') AS pj"
        )
        if reg["sr"] is None or reg["pj"] is None:
            return {"reconciled": 0, "stopped": 0, "failed": 0}
        rows = await connector.execute_query_all_async(
            _RECONCILE_SCAN_SQL,
            grace=get_settings().scan_run_reconcile_grace_seconds,
            scan_task=SCAN_TASK_NAME,
        )
    except Exception:  # noqa: BLE001 - reconciler must never fail the tick
        return {"reconciled": 0, "stopped": 0, "failed": 0}
    stopped = sum(1 for r in rows if r["status"] == "stopped")
    failed = sum(1 for r in rows if r["status"] == "failed")
    return {"reconciled": len(rows), "stopped": stopped, "failed": failed}


async def reap_stalled_jobs_now() -> dict:
    """Requeue or fail jobs orphaned in ``doing`` by a dead/restarted worker.

    Assumes the procrastinate app connector is already OPEN (the maintenance
    periodic task runs inside the worker where it is; the API endpoint wraps this
    in ``proc_app.open_async()``). Steps:

      1. ``prune_stalled_workers`` deletes worker rows with no recent heartbeat;
         the ``worker_id`` FK (``ON DELETE SET NULL``) nulls those workers' jobs,
         so the orphans surface in the heartbeat net below.
      2. Detect stalled ``doing`` jobs (:data:`_DETECT_STALLED_SQL`).
      3. ``scan_library`` orphans are FAILED and their running/stopping ScanRun is
         transitioned to ``failed`` (FIX-8: an OOM-killed scan never ran its own
         crash handler, so the ScanRun was left ``running`` and blocked the
         scheduler forever -- invariant 7). Every other orphan is RETRIED
         (``doing -> todo``, attempts+1) so it runs again on a live worker, UNLESS
         it has already burned ``reap_max_attempts`` (FIX-8: a job whose worker
         keeps dying is FAILED rather than requeued forever -- the live box saw
         attempts=50/51 from unbounded reaper requeues).

    KEY LOCK FINDING (load-bearing): the ``queueing_lock`` unique index is
    partial — ``WHERE status = 'todo'`` ONLY. A ``doing`` job therefore holds NO
    queueing lock; retrying it back to ``todo`` RE-establishes that lock. If a
    fresh periodic tick already enqueued a ``todo`` job holding the same lock (a
    scan/index/alert dedup lock), the retry collides on the partial-unique index
    and raises :class:`UniqueViolation`. We treat that as "a replacement is
    already queued" and FAIL the orphan instead — so the reaper is idempotent and
    never duplicates locked work.

    Returns ``{reaped, retried, failed, pruned_workers}`` (``reaped`` = retried +
    failed). Total on a bare DB (no procrastinate schema): returns all-zeros.
    """
    settings = get_settings()
    hb = settings.job_stall_heartbeat_seconds
    age = settings.job_stall_seconds
    manager = proc_app.job_manager
    connector = manager.connector

    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return {
            "reaped": 0, "retried": 0, "failed": 0, "pruned_workers": 0,
            "scan_runs_reconciled": 0,
        }

    pruned = await manager.prune_stalled_workers(seconds_since_heartbeat=hb)

    rows = await connector.execute_query_all_async(
        _DETECT_STALLED_SQL, hb=hb, age=age, scan_task=SCAN_TASK_NAME
    )

    retried = 0
    failed = 0
    now = datetime.now(UTC)
    reap_cap = settings.reap_max_attempts
    for row in rows:
        job_id = row["id"]
        if row["task"] == SCAN_TASK_NAME:
            # A stalled scan is FAILED (never silently re-run) AND its ScanRun is
            # transitioned running->failed (invariant 7 + unblocks the scheduler).
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            await _fail_scanrun_for_reaped_scan(row.get("args"))
            failed += 1
            continue
        # FIX-8: bound the requeue budget. A NON-scan job whose worker keeps dying
        # (OOM loop) would otherwise be requeued every reaper tick forever (the
        # live box saw attempts=50/51). Past the cap, FAIL it so it surfaces on the
        # failed-jobs list instead of looping. attempts is 0-based; >= cap means it
        # has already been (re)tried cap times.
        if (row.get("attempts") or 0) >= reap_cap:
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            failed += 1
            continue
        try:
            await manager.retry_job_by_id_async(job_id=job_id, retry_at=now)
            retried += 1
        except UniqueViolation:
            # A replacement todo job already holds this queueing_lock — fail the
            # orphan rather than duplicate the locked work (idempotent ticks).
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            failed += 1

    # FIX-15: after reaping stalled DOING jobs (which flips THEIR running/stopping
    # ScanRuns via _fail_scanrun_for_reaped_scan), sweep any remaining non-terminal
    # ScanRun whose job is already GONE (succeeded/failed/cancelled/purged) -- the
    # case the reaper's stalled-job nets can never see. Bounded, idempotent.
    reconciled = await reconcile_orphaned_scan_runs_now()

    return {
        "reaped": retried + failed,
        "retried": retried,
        "failed": failed,
        "pruned_workers": len(pruned),
        "scan_runs_reconciled": reconciled["reconciled"],
    }


# Every 5 minutes: prune dead workers and requeue/fail their orphaned jobs. The
# queueing_lock collapses overlapping ticks to a single queued run. Bounded,
# read-mostly, idempotent (a second run over the same state acts on nothing new).
@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reap_stalled_jobs",
    queueing_lock="reap-stalled-jobs",
)
async def reap_stalled_jobs(timestamp: int) -> dict:
    """Maintenance tick: reap stalled ``doing`` jobs (FIX-6). Returns the counts
    dict from :func:`reap_stalled_jobs_now` (runs inside the worker, where the
    procrastinate connector is already open)."""
    return await reap_stalled_jobs_now()


# --- FIX-8 (scan-scheduling storm): unfinished-scan dedupe ------------------
# The partial ``queueing_lock`` unique index only covers ``status='todo'`` (see
# the reaper's lock finding), so a scan job that has STARTED (``doing``) — or one
# stalled in ``doing`` because its worker died mid-scan — holds NO lock. The live
# box's storm: a worker OOMed before its ScanRun row committed, so neither the
# lock nor the running-ScanRun guard saw the stalled job, and every due tick
# re-deferred, stacking 5-6 duplicate scan jobs. This belt checks
# ``procrastinate_jobs`` directly for ANY unfinished scan_library job for the
# same (library_id, rel_path) across every non-terminal status.
#
# Procrastinate 3.9 statuses: todo, doing, succeeded, failed, cancelled,
# aborting, aborted. Non-terminal (a job that will or may still run) =
# {todo, doing, aborting}. We match args->>'library_id' and, for a FULL scan,
# require rel_path to be absent/NULL (a scoped job for a subtree must not dedupe
# a full-library scan and vice-versa).
_UNFINISHED_SCAN_SQL = """
SELECT 1
FROM procrastinate_jobs
WHERE task_name = %(task)s
  AND status IN ('todo', 'doing', 'aborting')
  AND args->>'library_id' = %(library_id)s
  AND (
        (%(rel_path)s::text IS NULL AND (args->>'rel_path') IS NULL)
     OR (args->>'rel_path') = %(rel_path)s::text
      )
LIMIT 1
"""


async def scan_job_pending(library_id: str, rel_path: str | None = None) -> bool:
    """True if an unfinished (todo/doing/aborting) scan_library job already
    exists for this (library_id, rel_path). Assumes the proc_app connector is
    reachable (the scheduler runs inside the worker where it is; API defer sites
    open it). Fails SAFE to ``False`` when the procrastinate schema is absent
    (bare DB / unit tests) so it never blocks legitimate scheduling."""
    connector = proc_app.job_manager.connector
    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return False
    rows = await connector.execute_query_all_async(
        _UNFINISHED_SCAN_SQL,
        task="filearr.tasks.scan.scan_library",
        library_id=str(library_id),
        rel_path=rel_path,
    )
    return len(rows) > 0


# --- FIX-15: is a scan RUN genuinely being processed right now? -------------
# ``scan_job_pending`` answers "does ANY unfinished job exist" (todo/doing/
# aborting) and is the FIX-9 scheduling dedupe. Force-clear and the hardened
# stop endpoint need a STRICTER question: is a LIVE worker actually draining this
# (library, scope) right now? That is a ``doing`` scan_library job whose worker
# has heart-beaten within the stall window -- exactly the case where a
# ``stopping`` marker WILL be observed and where refusing a force-clear is
# correct. A ``doing`` job with a stale/pruned worker is NOT active (it is an
# orphan the reaper will fail), and a ``todo``/``aborting`` job is not draining
# this run either. Fails SAFE to ``False`` (not active) when the procrastinate
# schema is absent (bare DB / unit tests) so a manual repair is never blocked by
# an unreachable queue.
_ACTIVE_SCAN_SQL = """
SELECT 1
FROM procrastinate_jobs j
JOIN procrastinate_workers w ON w.id = j.worker_id
WHERE j.task_name = %(task)s
  AND j.status = 'doing'
  AND w.last_heartbeat >= NOW() - make_interval(secs => %(hb)s)
  AND j.args->>'library_id' = %(library_id)s
  AND (
        (%(rel_path)s::text IS NULL AND (j.args->>'rel_path') IS NULL)
     OR (j.args->>'rel_path') = %(rel_path)s::text
      )
LIMIT 1
"""


async def scan_job_active(library_id: str, rel_path: str | None = None) -> bool | None:
    """Tri-state: is a LIVE worker currently draining a scan_library job for this
    (library_id, rel_path)?

      * ``True``  -- a ``doing`` job whose worker heart-beat is fresh (within
        ``job_stall_heartbeat_seconds``) exists: a stop WILL be observed and a
        force-clear must be refused.
      * ``False`` -- procrastinate schema present but NO such live job: the run is
        orphaned (its worker died, or its job already terminated).
      * ``None``  -- the procrastinate schema is absent (bare/queue-less DB, unit
        tests): activeness is UNKNOWN. Callers decide the safe default (the stop
        endpoint keeps the legacy graceful ``stopping`` path; force-clear allows
        the manual repair).

    Assumes the proc_app connector is reachable (callers open it)."""
    connector = proc_app.job_manager.connector
    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return None
    rows = await connector.execute_query_all_async(
        _ACTIVE_SCAN_SQL,
        task="filearr.tasks.scan.scan_library",
        hb=get_settings().job_stall_heartbeat_seconds,
        library_id=str(library_id),
        rel_path=rel_path,
    )
    return len(rows) > 0


async def defer_scan(
    library_id: str,
    *,
    rel_path: str | None = None,
    recursive: bool = True,
    queueing_lock: str | None = None,
    force: bool = False,
) -> int | None:
    """Enqueue a scan for a library, or a *scoped* scan of a subtree (P2-T6) or a
    single file (W9).

    ``recursive`` (W9) is threaded onto the ``scan_library`` job so a non-recursive
    directory scan walks only that dir's direct children. It is only passed as a
    job arg when ``False`` (the ``scan_library`` task defaults ``recursive=True``),
    so a full/hot-folder scan's job args stay byte-for-byte unchanged and old
    queued jobs remain back-compatible.

    ``queueing_lock`` guarantees at most one *queued* scan per lock: Procrastinate
    rejects a second defer with the same lock while one is still waiting in the
    ``todo`` state (the lock frees the moment the job starts running). This is
    what makes a duplicated/late periodic tick — or a tick racing a manual scan —
    idempotent. On a collision we return ``None`` rather than raising, so the
    caller treats it as "already scheduled".

    FIX-8: BEFORE deferring we also check :func:`scan_job_pending` for any
    unfinished scan_library job for the same (library_id, rel_path) — this closes
    the gap the ``todo``-only queueing_lock leaves open once a scan has STARTED or
    STALLED in ``doing`` (the storm the live box hit). ``force=True`` bypasses the
    dedupe for an explicit operator re-trigger. On a dedupe hit we return ``None``
    (same "already scheduled" contract as the lock collision).

    Lock granularity: a full-library scan defaults to ``scan:<library_id>``; a
    scoped scan (``rel_path`` given) defaults to ``scan:<library_id>:<rel_path>``
    so a hot-folder scan and the library's full scan queue independently, and two
    hot folders never collide with each other.
    """
    if queueing_lock is not None:
        lock = queueing_lock
    elif rel_path is not None:
        lock = f"scan:{library_id}:{rel_path}"
    else:
        lock = f"scan:{library_id}"
    kwargs: dict = {"library_id": library_id}
    if rel_path is not None:
        kwargs["rel_path"] = rel_path
    # Only carry the flag when non-default so existing (recursive) scan job args
    # are unchanged and pre-W9 queued jobs keep working (task default is True).
    if not recursive:
        kwargs["recursive"] = False
    async with proc_app.open_async():
        if not force and await scan_job_pending(library_id, rel_path):
            return None  # an unfinished scan for this scope already exists
        try:
            job = await proc_app.configure_task(
                "filearr.tasks.scan.scan_library",
                queue="scan",
                queueing_lock=lock,
                priority=get_settings().scan_priority,  # UI-T14 front-stage lane
            ).defer_async(**kwargs)
        except AlreadyEnqueued:
            return None
    return job


async def defer_index_sync(item_ids: list[str]) -> None:
    async with proc_app.open_async():
        await proc_app.configure_task(
            "filearr.tasks.index_sync.sync_items",
            queue="index",
            priority=get_settings().index_priority,  # UI-T14 default lane
        ).defer_async(item_ids=item_ids)


async def defer_thumb_item(item_id: str, tier: int) -> None:
    """Enqueue a ``thumb_item`` for one item + tier on the low-priority thumbs
    queue (P12 slice 2). Used by the serve endpoint on a VIDEO thumbnail miss:
    ffmpeg's latency variance must never run inline in a request handler, so a
    miss queues the frame-grab and 404s (the client retries).

    Reuses the connector when it is already open (worker context / tests) and
    only opens+closes its own connection when the caller's process has none (the
    API process, where proc_app is not held open) -- so it never tears down a
    shared pool that an enclosing ``open_async`` still needs."""
    settings = get_settings()

    def _deferrer():
        return proc_app.configure_task(
            "filearr.tasks.thumbs.thumb_item",
            queue=settings.queue_thumbnail,
            priority=settings.thumbs_priority,
        )

    try:
        await _deferrer().defer_async(item_id=item_id, tier=tier)
    except procrastinate.exceptions.AppNotOpen:
        async with proc_app.open_async():
            await _deferrer().defer_async(item_id=item_id, tier=tier)


async def defer_rebuild_index() -> int | None:
    """Defer a full shadow-swap ``rebuild_index`` on the ``index`` queue and return
    the Procrastinate job id (P9-T5). Used by ``POST /api/v1/system/rebuild-index``
    so an operator can trigger a rebuild/settings-migration rollout on demand
    instead of deferring the task by hand."""
    async with proc_app.open_async():
        job = await proc_app.configure_task(
            "filearr.tasks.index_sync.rebuild_index",
            queue="index",
            priority=get_settings().index_priority,  # UI-T14 default lane
        ).defer_async()
    return job


async def defer_extract(item_ids: list[str]) -> None:
    """Batch-defer extract jobs from OUTSIDE the worker (e.g. the retry-extracts
    API action).

    The scan task's ``_defer_extract_batch`` helper assumes the procrastinate app
    is already open (it runs inside a worker); the API is not, so this opens the
    connection around the same helper. No-op on an empty list."""
    if not item_ids:
        return
    from filearr.tasks.scan import _defer_extract_batch

    async with proc_app.open_async():
        await _defer_extract_batch(item_ids)


async def defer_embed(item_ids: list[str]) -> None:
    """Batch-defer ``embed_item`` jobs from OUTSIDE the worker (P3-T8), on the
    ``embed`` queue at the lowest priority. No-op on an empty list or when
    semantic search is disabled (the task itself also no-ops defensively)."""
    if not item_ids:
        return
    settings = get_settings()
    if not settings.semantic_enabled:
        return
    async with proc_app.open_async():
        deferrer = proc_app.configure_task(
            "filearr.tasks.embed.embed_item",
            queue=settings.queue_embed,
            priority=settings.embed_priority,
        )
        for iid in item_ids:
            await deferrer.defer_async(item_id=iid)


async def defer_embed_missing() -> int | None:
    """Defer the ``embed_missing`` backfill on the ``embed`` queue and return its
    Procrastinate job id (P3-T8). Backs ``POST /api/v1/system/embed-backfill``."""
    async with proc_app.open_async():
        job = await proc_app.configure_task(
            "filearr.tasks.embed.embed_missing",
            queue=get_settings().queue_embed,
            priority=get_settings().embed_priority,
        ).defer_async()
    return job


@proc_app.periodic(cron="0 4 * * *")
# FIX-8: periodic maintenance tasks carry NO retry -- a transient failure is
# simply re-run on the next tick, and self-retry here was one source of the
# runaway attempts (50/51) the reaper then compounded.
@proc_app.task(queue="maintenance", name="filearr.worker.purge_recycle_bin")
async def purge_recycle_bin(timestamp: int) -> int:
    """Hard-delete trashed items past the retention window (recycle-bin purge).

    P5-T5 purge-safety watermark (§4.5): a trashed item OWNED BY A LIVE AGENT is
    held back until that agent's last full-manifest reconciliation has observed
    the deletion — i.e. ``agents.last_reconcile_at >= items.deleted_at`` (the
    ``deleted_at`` timestamp IS the trashed-transition instant, the same key the
    retention cutoff uses). A never-reconciled live agent (``last_reconcile_at``
    NULL) blocks its trashed items indefinitely; a revoked/deleted agent (or a
    non-agent local item, ``source_agent_id`` NULL) never blocks purge (R2)."""
    from sqlalchemy import and_, delete, or_, select

    from filearr.db import SessionLocal
    from filearr.models import Agent, Item, ItemStatus
    from filearr.search import delete_docs

    cutoff = datetime.now(UTC) - timedelta(days=get_settings().recycle_retention_days)
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Item.id)
            .outerjoin(Agent, Agent.id == Item.source_agent_id)
            .where(
                Item.status == ItemStatus.trashed,
                Item.deleted_at < cutoff,
                or_(
                    Item.source_agent_id.is_(None),      # local item — no agent gate
                    Agent.id.is_(None),                  # dangling/unknown agent ref
                    Agent.revoked_at.isnot(None),        # revoked agent never blocks
                    and_(                                # reconciled past the deletion
                        Agent.last_reconcile_at.isnot(None),
                        Agent.last_reconcile_at >= Item.deleted_at,
                    ),
                ),
            )
        )
        ids = [str(i) for (i,) in rows]
        if ids:
            await session.execute(delete(Item).where(Item.id.in_(ids)))
            await session.commit()
            await delete_docs(ids)
    return len(ids)


# --- P4-T9: ItemVersion audit-retention purge ------------------------------
# Bounds unbounded per-rescan audit growth: extractor-sourced version rows
# (source != 'user') older than FILEARR_ITEM_VERSION_RETENTION_DAYS are hard-
# deleted. source='user' (manual API/UI edit) rows are EXEMPT and never touched
# regardless of age. Runs at 04:15, between the 04:00 recycle-bin purge and the
# 04:30 nightly reconcile, so the three maintenance jobs never overlap. Mirrors
# the recycle-bin purge shape (periodic maintenance task). Never touches
# Meilisearch (audit rows are not projected). FIX-8: no retry -- a transient DB
# fault is simply retried on the next daily tick.
@proc_app.periodic(cron="15 4 * * *")
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_item_versions"  # FIX-8: no retry
)
async def purge_item_versions(timestamp: int) -> int:
    """Hard-delete non-'user' ItemVersion rows past the retention window (P4-T9).
    source='user' rows are exempt. Returns the number of rows deleted."""
    from sqlalchemy import delete

    from filearr.db import SessionLocal
    from filearr.models import ItemVersion

    cutoff = datetime.now(UTC) - timedelta(
        days=get_settings().audit_retention_days
    )
    async with SessionLocal() as session:
        result = await session.execute(
            delete(ItemVersion).where(
                ItemVersion.source != "user", ItemVersion.changed_at < cutoff
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- P8-T14: alert_events retention purge -----------------------------------
# Keeps alert_events bounded (mirrors the recycle-bin / audit purges, invariant
# 4). Deletes only TERMINAL rows older than FILEARR_ALERT_EVENTS_RETENTION_DAYS:
# delivered=true OR retries-exhausted (delivery_attempts >= max). A PENDING alert
# (still deliverable) or a ceiling-HELD row (attempts untouched) is NEVER purged
# regardless of age -- we never silently drop an undelivered alert. Runs at 04:45,
# clear of the 04:00/04:15/04:30 maintenance jobs. FIX-8: no retry -- a transient
# DB fault is retried on the next daily tick; alert_events are never projected.
@proc_app.periodic(cron="45 4 * * *")
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_alert_events"  # FIX-8: no retry
)
async def purge_alert_events(timestamp: int) -> int:
    """Hard-delete terminal alert_events past the retention window (P8-T14).
    Delivered OR retries-exhausted only; pending/held rows are exempt. Returns the
    number of rows deleted."""
    from sqlalchemy import delete, or_

    from filearr.db import SessionLocal
    from filearr.models import AlertEvent

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.alert_events_retention_days)
    max_attempts = settings.alert_max_delivery_attempts
    async with SessionLocal() as session:
        result = await session.execute(
            delete(AlertEvent).where(
                AlertEvent.occurred_at < cutoff,
                or_(
                    AlertEvent.delivered.is_(True),
                    AlertEvent.delivery_attempts >= max_attempts,
                ),
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- P6-T9: security_events retention purge ---------------------------------
# Keeps the audit log bounded (invariant-4 discipline). Noisy ``login_failure``
# rows are purged after the shorter FILEARR_SECURITY_AUDIT_FAILURE_RETENTION_DAYS
# window; every other (higher-value) event is kept for
# FILEARR_SECURITY_AUDIT_RETENTION_DAYS. Runs at 04:20, clear of the other
# maintenance jobs. FIX-8: no retry — a transient DB fault is retried next daily
# tick; security_events are append-only and never projected.
@proc_app.periodic(cron="20 4 * * *")
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_security_events"  # no retry
)
async def purge_security_events(timestamp: int) -> int:
    """Hard-delete security_events past their per-class retention window (P6-T9).
    Returns the number of rows deleted."""
    from sqlalchemy import and_, delete, or_

    from filearr.db import SessionLocal
    from filearr.models import SecurityEvent

    settings = get_settings()
    now = datetime.now(UTC)
    failure_cutoff = now - timedelta(days=settings.security_audit_failure_retention_days)
    other_cutoff = now - timedelta(days=settings.security_audit_retention_days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(SecurityEvent).where(
                or_(
                    and_(
                        SecurityEvent.event_type == "login_failure",
                        SecurityEvent.ts < failure_cutoff,
                    ),
                    and_(
                        SecurityEvent.event_type != "login_failure",
                        SecurityEvent.ts < other_cutoff,
                    ),
                )
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- FIX-8: procrastinate job-history retention -----------------------------
# The failed-jobs list on the Admin + Jobs pages (and the succeeded-job backlog
# that powers the queue-card "done" counters + the extract-rate ETA) grew
# UNBOUNDED — procrastinate never purges terminal rows on its own. This daily
# maintenance task hard-deletes terminal rows (succeeded / failed / cancelled /
# aborted) whose most recent event is older than
# FILEARR_JOB_HISTORY_RETENTION_DAYS, via the vetted JobManager.delete_old_jobs
# query (age is measured from the latest procrastinate_events row — there is no
# finished_at column on procrastinate_jobs). todo/doing jobs are NEVER touched
# (they are not final states). Runs at 04:50, clear of the other 04:xx
# maintenance jobs. Deleted counts are logged so an operator can see the trim.
#
# Count (grouped by final status) of the rows delete_old_jobs would remove, for
# the log line. Mirrors procrastinate's delete_old_jobs predicate EXACTLY (latest
# event `at` older than nb_hours, final statuses only) so the logged numbers
# match what is deleted.
_PURGE_COUNT_SQL = """
SELECT job.status::text AS status, count(*) AS n
FROM (
    SELECT DISTINCT ON (j.id) j.id, j.status, e.at AS latest_at
    FROM procrastinate_jobs j
    JOIN procrastinate_events e ON j.id = e.job_id
    ORDER BY j.id, e.at DESC
) job
WHERE job.status = ANY(
        ARRAY['succeeded','failed','cancelled','aborted']::procrastinate_job_status[]
      )
  AND job.latest_at < NOW() - make_interval(hours => %(nb_hours)s)
GROUP BY job.status
"""


async def purge_job_history_now() -> dict:
    """Hard-delete terminal procrastinate rows older than the retention window.

    Assumes the procrastinate app connector is already OPEN (the maintenance
    periodic runs inside the worker, where it is — same contract as
    :func:`reap_stalled_jobs_now`). Uses the vetted
    ``JobManager.delete_old_jobs`` with every terminal status enabled; a
    count-before pass (the same predicate) feeds the log line. NEVER touches
    todo/doing jobs (they are not final states, so the status filter excludes
    them). Total on a bare DB (no procrastinate schema): returns all-zeros.

    Returns ``{deleted, by_status}`` where ``by_status`` maps each final status
    to how many rows aged out this run.
    """
    import logging

    manager = proc_app.job_manager
    connector = manager.connector

    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return {"deleted": 0, "by_status": {}}

    nb_hours = get_settings().job_history_retention_days * 24
    rows = await connector.execute_query_all_async(_PURGE_COUNT_SQL, nb_hours=nb_hours)
    by_status = {row["status"]: int(row["n"]) for row in rows}
    deleted = sum(by_status.values())

    await manager.delete_old_jobs(
        nb_hours,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )

    if deleted:
        logging.getLogger("filearr.worker").info(
            "purge_job_history: deleted %d terminal job rows older than %dd (%s)",
            deleted,
            get_settings().job_history_retention_days,
            ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
        )
    return {"deleted": deleted, "by_status": by_status}


@proc_app.periodic(cron="50 4 * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_job_history",
    queueing_lock="purge-job-history",  # FIX-8: no retry (periodic re-runs)
)
async def purge_job_history(timestamp: int) -> int:
    """Maintenance tick: hard-delete terminal procrastinate rows past the
    retention window (FIX-8). Returns the number of rows deleted."""
    return (await purge_job_history_now())["deleted"]


# --- QH-T4: small-file re-hash sweep ----------------------------------------
# The QH-T1..T3 hashing fix (quick_hash 64-128 KiB partial-read repair, small-file
# unconditional content_hash, xxh3-64 -> xxh3-128) changed the stored hashes for
# every file <= 128 KiB, but policy_version fingerprints CONFIG, not hashing
# behavior, so the ~3,930-group (brief §3a) backlog of items hashed under the old
# algorithm would never re-hash on their own (the scan self-heal only re-queues
# quick_hash IS NULL rows — it can't tell "never hashed" from "hashed under a
# since-fixed algorithm"). This nightly, rate-limited sweep re-enqueues a bounded
# batch of active, size <= 128 KiB items still on the OLD provenance scheme through
# the NORMAL extract path (which recomputes both hashes correctly and re-stamps
# policy_version = cfg2). Once re-extracted, an item's cfg2 fingerprint excludes it
# next tick -> idempotent convergence, no blocking data migration.
#
# ARCHITECT RULING (binding): agent-owned items (library.source_agent_id set) are
# EXCLUDED — central cannot open an agent's files, so it cannot re-hash them; they
# correct via the agent's own rescan (a hash change re-emits modified events for
# the band) + replication. The sweep also NEVER touches items outside the affected
# size <= 128 KiB band: a >128 KiB file's sampled quick_hash was never wrong, and
# its content_hash migrates lazily on natural rescan.
_SMALL_FILE_CEILING = 2 * 65536  # 128 KiB — mirrors extract.QUICK_CHUNK*2
# Bounded per-tick enqueue so a large backlog never spikes the extract queue on
# deploy. A module constant (not a config knob — config.py is out of scope here);
# ~4,000 backlog / 1,000 per nightly tick converges in a few nights.
REHASH_SWEEP_BATCH = 1000


async def rehash_small_files_now() -> dict:
    """Re-enqueue a bounded batch of active <=128 KiB items still on an old
    provenance scheme through the normal extract path (QH-T4). Excludes agent-owned
    items (architect ruling) and never touches files outside the affected band.
    Idempotent: a re-extracted item advances to cfg2 and drops out next run.
    Returns ``{requeued}``."""
    from sqlalchemy import select

    from filearr.db import SessionLocal
    from filearr.models import Item, ItemStatus, Library
    from filearr.provenance import _SCHEME
    from filearr.tasks.scan import _defer_extract_batch

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Item.id)
            .join(Library, Library.id == Item.library_id)
            .where(
                Item.status == ItemStatus.active,
                Item.size <= _SMALL_FILE_CEILING,
                # Extracted under an OLD scheme (non-null, not the current prefix).
                # A NULL policy_version is an unextracted row handled by the normal
                # extract/self-heal path — not this migration's concern.
                Item.policy_version.isnot(None),
                ~Item.policy_version.like(f"{_SCHEME}:%"),
                Library.source_agent_id.is_(None),  # ruling: never sweep agent items
            )
            .order_by(Item.id)
            .limit(REHASH_SWEEP_BATCH)
        )
        ids = [str(i) for (i,) in rows]
    if ids:
        # _defer_extract_batch assumes the proc app is open; mirror defer_extract.
        async with proc_app.open_async():
            await _defer_extract_batch(ids)
    return {"requeued": len(ids)}


@proc_app.periodic(cron="55 4 * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.rehash_small_files",
    queueing_lock="rehash-small-files",  # FIX-8: no retry (nightly re-runs)
)
async def rehash_small_files(timestamp: int) -> int:
    """Maintenance tick: re-enqueue a bounded batch of active <=128 KiB items on an
    old provenance scheme through the normal extract path (QH-T4). Runs at 04:55,
    clear of the other 04:xx maintenance jobs. Returns the number re-queued."""
    return (await rehash_small_files_now())["requeued"]


@proc_app.periodic(cron="30 4 * * *")
@proc_app.task(queue="maintenance", name="filearr.worker.nightly_reconcile")  # FIX-8: no retry
async def nightly_reconcile(timestamp: int) -> None:
    """Safety net: re-sync the whole search index from Postgres (projection is disposable)."""
    from filearr.tasks.index_sync import rebuild_index

    await rebuild_index()


# --- P9-T7: hourly Postgres<->Meili reconciliation sweep --------------------
# Bounded worst-case index staleness even if every incremental index_sync update
# (or, once P9-T6 lands, every task webhook — Meili webhooks have NO delivery
# retry) was lost. Runs at minute 7 to stay clear of the every-minute scan tick
# and the 04:xx purge/nightly-rebuild window. `queueing_lock` guarantees at most
# one sweep is ever queued: procrastinate's periodic deferrer catches the
# AlreadyEnqueued collision and skips (so a long sweep is never piled onto).
# FIX-8: no retry -- a lost/failed sweep is simply re-run next hour regardless.
@proc_app.periodic(cron="7 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reconcile_meili",
    queueing_lock="reconcile-meili",  # FIX-8: no retry (hourly re-runs)
)
async def reconcile_meili(timestamp: int) -> dict:
    """Detect and repair Postgres<->Meili divergence (P9-T7). NEVER writes Postgres."""
    from filearr.tasks.reconcile import run_reconcile_sweep

    return await run_reconcile_sweep()


# --- P9-T5: orphaned shadow-index reaper ------------------------------------
# A crashed or retried shadow-swap rebuild can leave an orphaned `<index>_rebuild_
# <epoch>` shadow index on disk (holding a full extra copy — real disk cost). This
# hourly sweep deletes shadows older than FILEARR_MEILI_SHADOW_MAX_AGE_HOURS (6h)
# by their epoch-embedded name, so a live in-flight rebuild's young shadow is never
# reaped mid-build. Runs at minute 47 to stay clear of the scan tick, the 04:xx
# purge/rebuild window, and the :07 reconcile sweep. queueing_lock collapses any
# duplicate enqueue. FIX-8: no retry -- the hourly tick re-runs on any fault.
@proc_app.periodic(cron="47 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reap_shadow_indexes",
    queueing_lock="reap-shadow-indexes",  # FIX-8: no retry (hourly re-runs)
)
async def reap_shadow_indexes(timestamp: int) -> int:
    """Delete orphaned shadow indexes from crashed/retried rebuilds (P9-T5).
    Returns the number reaped. Meili-only; never touches Postgres."""
    from filearr.meili_ops import reap_stale_shadows

    return len(await reap_stale_shadows())


# --- T5: cron-scheduled scanning -------------------------------------------
# One static, import-time periodic task on a 1-minute tick (Procrastinate cannot
# register periodic tasks dynamically, so per-library cron is evaluated here in
# code rather than via one periodic task per library). No worker restart is
# needed when a library's scan_cron changes -- the next tick simply reads the new
# value. `timestamp` is the Unix time of the tick that fired this run and is used
# both as the cron reference minute and as Procrastinate's periodic dedup key
# (a given minute defers at most once, even if the scheduler double-fires).
async def _defer_due_scans(tick: datetime) -> list[str]:
    """Defer every scan due at ``tick`` and return their scheduling keys.

    Two kinds of due work are evaluated on this one static tick (P2-T6 rides T5's
    single tick — Procrastinate periodic tasks are import-time static):

      * **Full-library scans** — a library whose ``scan_cron`` is due. Key is
        ``str(library.id)``. Skipped if ANY scan (full or scoped) is currently
        running for the library: the running-row query below covers both, so a
        long scan is never piled onto.
      * **Scoped (hot-folder) scans** — an enabled ``scan_paths`` row whose own
        ``scan_cron`` (NOT the inherited library one) is due. Key is
        ``"<library.id>:<rel_path>"``. Skipped while a FULL scan is running for
        the library (the full scan already covers the subtree — "full-scan lock
        wins"), and skipped while a scoped scan of the *same* rel_path is running.
        A scoped scan does NOT block a differently-scoped scan or the full sched.

    The queueing lock in :func:`defer_scan` additionally collapses any
    duplicate/racing enqueue for the same lock. A library with zero ``scan_paths``
    rows behaves exactly as T5 (regression guard)."""
    from sqlalchemy import select

    from filearr.db import SessionLocal
    from filearr.models import Library, ScanPath, ScanRun
    from filearr.schedule import due_occurrence

    cap = get_settings().scan_schedule_max_catchup_minutes
    deferred: list[str] = []
    async with SessionLocal() as session:
        # All enabled libraries (not just those with a library-level cron): a
        # library may schedule only via scan_paths rows. P5-T4: agent-owned
        # libraries (source_agent_id NOT NULL) are EXCLUDED — central never scans
        # a remote agent's corpus; its content arrives via the replication apply
        # path, so a cron/hot-folder scan against a non-existent local root would
        # only tombstone the whole replicated catalog.
        libraries = list(
            (
                await session.execute(
                    select(Library).where(
                        Library.enabled.is_(True),
                        Library.source_agent_id.is_(None),
                    )
                )
            ).scalars()
        )
        for library in libraries:
            # One query for this library's running scans; classify by scope so we
            # can tell a running FULL scan (rel_path IS NULL) from scoped ones.
            running_scopes = list(
                (
                    await session.execute(
                        select(ScanRun.rel_path).where(
                            ScanRun.library_id == library.id,
                            ScanRun.status.in_(("running", "stopping")),
                        )
                    )
                ).scalars()
            )
            any_running = bool(running_scopes)
            full_running = any(rp is None for rp in running_scopes)
            busy_scopes = {rp for rp in running_scopes if rp is not None}

            # --- library-level full scan (FIX-8 once-per-occurrence) ---
            # Fire only for a cron occurrence strictly newer than the one this
            # library last consumed, and stamp it BEFORE the enqueue (committed
            # first, then defer) so a given occurrence fires at most once even
            # across duplicate/late ticks or a mid-scan worker death. If a scan is
            # currently running we do NOT consume the occurrence — it stays due and
            # fires (collapsed to the latest) once the running scan clears.
            occ = (
                due_occurrence(
                    library.scan_cron, tick, library.last_cron_fired_at,
                    max_catchup_minutes=cap,
                )
                if library.scan_cron
                else None
            )
            if occ is not None and not any_running:
                library.last_cron_fired_at = occ
                await session.commit()
                job = await defer_scan(str(library.id))
                if job is not None:
                    deferred.append(str(library.id))

            # --- per-path scoped scans (only rows carrying their OWN cron) ---
            scan_paths = list(
                (
                    await session.execute(
                        select(ScanPath).where(
                            ScanPath.library_id == library.id,
                            ScanPath.enabled.is_(True),
                            ScanPath.scan_cron.isnot(None),
                        )
                    )
                ).scalars()
            )
            for sp in scan_paths:
                sp_occ = due_occurrence(
                    sp.scan_cron, tick, sp.last_cron_fired_at,
                    max_catchup_minutes=cap,
                )
                if sp_occ is None:
                    continue
                if full_running:
                    continue  # full-scan lock wins; the full scan covers this subtree
                if sp.rel_path in busy_scopes:
                    continue  # a scoped scan of this exact subtree is already running
                sp.last_cron_fired_at = sp_occ  # consume in the enqueue commit
                await session.commit()
                job = await defer_scan(str(library.id), rel_path=sp.rel_path)
                if job is not None:
                    deferred.append(f"{library.id}:{sp.rel_path}")
    return deferred


# --- P11-T9: scheduled report delivery + P11-T11 export lifecycle -----------
# Rides the SAME minutely tick contract as scan scheduling: once-per-occurrence
# firing via each schedule's persisted last_cron_fired_at (FIX-8/FIX-9). No
# retry (a transient failure re-evaluates next minute); the queueing_lock
# collapses overlapping ticks. Purge + reconcile ride the nightly maintenance
# lane so a crashed export never sits `running` and an expired artifact is freed.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.schedule_report_exports",
    queueing_lock="schedule-report-exports",  # FIX-8: no retry (minutely re-runs)
)
async def schedule_report_exports(timestamp: int) -> int:
    """Evaluate every enabled report schedule against this minute and enqueue an
    export for each due (un-consumed) occurrence (P11-T9)."""
    from filearr.tasks.reports import evaluate_report_schedules

    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    return len(await evaluate_report_schedules(tick))


@proc_app.periodic(cron="40 4 * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_report_exports",
    queueing_lock="purge-report-exports",  # FIX-8: no retry
)
async def purge_report_exports(timestamp: int) -> int:
    """Delete expired export artifacts (row retained, ``purged_at`` stamped) and
    reconcile any export stuck ``running`` past its timeout to ``failed``
    (invariant 7). Returns the number of artifacts purged (P11-T11)."""
    from filearr import exports

    async with SessionLocal() as session:
        await exports.reconcile_stale_exports(session, get_settings())
        return await exports.purge_expired_exports(session, get_settings())


@proc_app.periodic(cron="*/10 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reconcile_report_exports",
    queueing_lock="reconcile-report-exports",  # FIX-8: no retry
)
async def reconcile_report_exports(timestamp: int) -> int:
    """Flip a crashed export stuck ``running`` to ``failed`` every 10 minutes
    (invariant 7 — never leave a job ``running``)."""
    from filearr import exports

    async with SessionLocal() as session:
        return await exports.reconcile_stale_exports(session, get_settings())


@proc_app.periodic(cron="* * * * *")
@proc_app.task(queue="maintenance", name="filearr.worker.schedule_scans")
async def schedule_scans(timestamp: int) -> int:
    """Evaluate every enabled library's (and hot folder's) schedule against this
    minute's tick and defer scans for occurrences not yet consumed. FIX-8: firing
    is once-per-occurrence (persisted ``last_cron_fired_at``), deduped against any
    unfinished scan_library job, and never re-fires a missed occurrence per tick.
    Returns the number of scans deferred."""
    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    deferred = await _defer_due_scans(tick)
    return len(deferred)


# --- T5: watch-mode supervisor entrypoint ----------------------------------
# --- P8-T6/T7/T8/T15: alert dispatch pump -----------------------------------
# One minutely tick drives the state-derived alert pump. Group-wait, group-
# interval, repeat-interval, digest windowing and the per-rule hourly ceiling are
# ALL derived from the alert_events rows every tick (no separate scheduler/state
# table), so a duplicated/late tick simply re-derives the same decision. The
# queueing_lock collapses overlapping ticks to at most one queued run.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="alerts",
    name="filearr.worker.pump_alerts",
    queueing_lock="pump-alerts",
    priority=get_settings().alerts_priority,  # UI-T14: user-facing timeliness
)
async def pump_alerts(timestamp: int) -> dict:
    from filearr.tasks.alerts import dispatch_pending

    return await dispatch_pending(timestamp)


async def _defer_scan_if_idle(library_id: str, rel_path: str | None = None) -> int | None:
    """Defer a scan for ``library_id`` unless a conflicting scan is already
    running (watch-mode trigger). The queueing lock in :func:`defer_scan`
    additionally collapses a burst of watcher events into a single queued scan.

    ``rel_path`` (P2-T6): when a per-path watcher fires, defer a *scoped* scan of
    that subtree. Conflict rules mirror the scheduler: a full watcher (rel_path
    None) defers only if NO scan is running for the library; a scoped watcher
    defers unless a FULL scan is running (full covers the subtree) or a scoped
    scan of the SAME subtree is already running."""
    from sqlalchemy import select

    from filearr.db import SessionLocal
    from filearr.models import ScanRun

    async with SessionLocal() as session:
        running_scopes = list(
            (
                await session.execute(
                    select(ScanRun.rel_path).where(
                        ScanRun.library_id == library_id,
                        ScanRun.status.in_(("running", "stopping")),
                    )
                )
            ).scalars()
        )
    if rel_path is None:
        if running_scopes:  # any scan running -> full watcher stays idle
            return None
        return await defer_scan(library_id)
    # scoped watcher: full-scan lock wins; same-subtree scoped scan blocks too.
    if any(rp is None for rp in running_scopes):
        return None
    if rel_path in {rp for rp in running_scopes if rp is not None}:
        return None
    return await defer_scan(library_id, rel_path=rel_path)


# --- P10-T1: agent_commands TTL + redelivery sweep -------------------------
# The on-demand command primitive's maintenance tick (research §3.1): flip stale
# `pending` / lease-lapsed `picked_up` rows to `expired` (kept, not deleted, so
# the UI can say "the agent never came back") and re-queue unacked deliveries to
# `pending` (at-least-once), bounded by FILEARR_AGENT_COMMAND_MAX_ATTEMPTS. Runs
# every minute so a picked-up-then-dropped command redelivers within one interval.
# FIX-8/FIX-9 discipline: NO retry (a transient DB fault is retried on the next
# minute tick), `queueing_lock` collapses overlapping ticks to one queued run,
# and the whole thing is a cheap no-op when agents are disabled. Bounded per run.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.expire_agent_commands",  # FIX-9: no retry (periodic re-runs)
    queueing_lock="expire-agent-commands",
)
async def expire_agent_commands(timestamp: int) -> dict:
    """Expire past-TTL agent commands + re-queue unacked-past-lease deliveries."""
    settings = get_settings()
    if not settings.agents_enabled:
        return {"skipped": "agents disabled"}
    from filearr.agentsync import run_agent_command_sweep
    from filearr.db import SessionLocal

    async with SessionLocal() as session:
        return await run_agent_command_sweep(
            session,
            now=datetime.now(UTC),
            lease_seconds=settings.agent_command_lease_seconds,
            max_attempts=settings.agent_command_max_attempts,
        )


# --- P10-T8: staging TTL cleanup sweep -------------------------------------
# Bounds central staging disk (research §5): reaps ``staging_transfers`` rows +
# their staged files that are past ``expires_at`` (unless a download is actively
# draining them, watermarked by ``last_range_request_at``) and reclaims abandoned
# partial uploads (no progress for FILEARR_STAGING_ABANDONED_UPLOAD_SECONDS) on
# their own shorter schedule. Every 5 minutes so a completed retrieve's staged
# file is freed reasonably soon after its TTL without cutting an in-flight
# download. FIX-8/FIX-9 discipline: NO retry (a transient fault re-runs on the
# next tick), ``queueing_lock`` collapses overlapping ticks, bounded per run. NOT
# gated on ``agents_enabled`` — staged bytes on disk must be reclaimed even if the
# agent fleet was later disabled (the query is a cheap no-op on an empty table).
@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.cleanup_staging_transfers",  # FIX-9: no retry (periodic re-runs)
    queueing_lock="cleanup-staging-transfers",
)
async def cleanup_staging_transfers(timestamp: int) -> dict:
    """Reap dead/abandoned staging transfers + their files (P10-T8)."""
    from filearr.db import SessionLocal
    from filearr.staging_sweep import run_staging_cleanup_sweep

    settings = get_settings()
    async with SessionLocal() as session:
        return await run_staging_cleanup_sweep(
            session,
            now=datetime.now(UTC),
            download_grace_seconds=settings.staging_download_grace_seconds,
            abandoned_upload_seconds=settings.staging_abandoned_upload_seconds,
        )


def build_watch_supervisor():
    """Construct a :class:`WatchSupervisor` bound to the app DB + scan trigger."""
    from filearr.db import SessionLocal
    from filearr.watch import WatchSupervisor

    return WatchSupervisor(SessionLocal, _defer_scan_if_idle)


async def run_watch_supervisor() -> None:
    """Run the watch-mode supervisor loop (a companion to the Procrastinate
    worker process). It reconciles watchers against library config on a timer, so
    toggling watch_mode or editing a root takes effect without a restart."""
    await build_watch_supervisor().run()
