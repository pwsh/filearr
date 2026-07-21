"""Read-only introspection of *in-flight* and *composite* job state (UI-T10).

Companion to :mod:`filearr.queue_stats` (per-queue rollup) and
:mod:`filearr.meili_stats` (projection drift). Where those answer "how deep is
the backlog" and "is the index in sync", this answers "what is the worker doing
*right now*" and bundles every dashboard signal into one composable call so the
Jobs tab polls a single URL.

Everything here is a single cheap read over ``procrastinate_jobs`` /
``procrastinate_events`` / ``scan_runs`` — no writes, no per-row scans, no job
mutation. On a DB without the procrastinate schema (fresh DB before init_db ran
apply_schema) the running-jobs list degrades to ``[]`` rather than raising, so
the endpoint stays total.

Security note: procrastinate job ``args`` are NOT surfaced blindly — they can
carry arbitrary deferred-call kwargs. Only an ALLOWLIST of known reference keys
(:data:`_ARG_ALLOWLIST`) crosses the API boundary; every other key (including
any large list payloads like ``item_ids``) is dropped. The allowlisted values
are UUID/rel_path references that the UI renders as *text* (Svelte auto-escapes),
so a crafted rel_path cannot inject markup.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Float, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.errors import failed_jobs, retry_cap_for
from filearr.meili_stats import meili_snapshot
from filearr.models import Item, Library, ScanRun
from filearr.queue_stats import queue_snapshot

# Known reference keys we are willing to echo from a job's ``args``. Restricting
# to these keeps the payload small and predictable and never dumps opaque or
# bulk kwargs (e.g. ``item_ids`` lists on index-sync jobs).
_ARG_ALLOWLIST = ("item_id", "library_id", "scan_run_id", "rel_path")

# Fully-qualified library-scan task name. Exempt from the reaper's absolute-age
# stall net (a full walk legitimately runs long), so ``stalled`` below never
# flags a healthy long scan on age alone (FIX-6 — mirrors worker.SCAN_TASK_NAME).
_SCAN_TASK = "filearr.tasks.scan.scan_library"

# Hard cap on how many in-flight jobs we return (there are rarely more than the
# worker's concurrency, but the cap bounds the response regardless).
_RUNNING_CAP = 50


def _short_task(task_name: str | None) -> str:
    """Last dotted segment of a fully-qualified task name (the human-facing bit).

    ``"filearr.tasks.extract.extract_item"`` -> ``"extract_item"``.
    """
    if not task_name:
        return ""
    return task_name.rsplit(".", 1)[-1]


def _allowlist_args(args: object) -> dict:
    """Keep only known reference keys from a job's ``args`` mapping."""
    if not isinstance(args, dict):
        return {}
    return {k: args[k] for k in _ARG_ALLOWLIST if k in args}


async def running_jobs(session: AsyncSession, *, limit: int = _RUNNING_CAP) -> list[dict]:
    """Currently-executing jobs (``status = 'doing'``), newest first, capped.

    Shape ``[{id, queue, task, args, started_at, seconds_running, attempts,
    retry_cap, worker_id, worker_alive, stalled, rel_path, size, library_name}]``:

    * ``task`` is the short task name (last dotted segment).
    * ``retry_cap`` is the task's genuine-failure retry budget (FIX-12), so the
      UI can render ``attempts/cap`` and show any excess as reschedules/requeues
      rather than failed tries (extract's staged-gate waits inflate ``attempts``).
    * ``args`` is the allowlisted reference subset only (see module note).
    * ``started_at`` is the timestamp of the job's most recent ``started`` event
      (null if the events table is absent), and ``seconds_running`` is derived
      from it (null when unknown).
    * ``rel_path`` is enriched for jobs carrying an ``item_id`` — resolved with a
      SINGLE ``IN`` query over ``items`` so humans see filenames, not UUIDs
      (null when the item is gone or the job has no item_id).

    Returns ``[]`` when the procrastinate schema is absent (total on fresh DBs).
    """
    limit = max(1, min(limit, _RUNNING_CAP))

    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return []

    events_exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_events')"))
    ).scalar()
    # Most recent 'started' event is the best proxy for "running since".
    started_expr = (
        "(SELECT max(e.at) FROM procrastinate_events e "
        " WHERE e.job_id = j.id AND e.type = 'started')"
        if events_exists is not None
        else "NULL"
    )

    # FIX-6: heartbeat freshness of each job's worker (null worker => orphaned).
    # A per-row scalar subselect keeps this a single query; procrastinate_workers
    # is tiny (bounded by worker concurrency).
    rows = (
        await session.execute(
            text(
                "SELECT j.id::text AS id, j.queue_name AS queue, j.task_name AS task, "
                "       j.args AS args, j.attempts AS attempts, j.worker_id AS worker_id, "
                "       (SELECT w.last_heartbeat FROM procrastinate_workers w "
                "        WHERE w.id = j.worker_id) AS worker_heartbeat, "
                f"      {started_expr} AS started_at "
                "FROM procrastinate_jobs j "
                "WHERE j.status = 'doing' "
                "ORDER BY j.id DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()

    settings = get_settings()
    hb = settings.job_stall_heartbeat_seconds
    age_limit = settings.job_stall_seconds
    now = datetime.now(UTC)
    jobs: list[dict] = []
    wanted_item_ids: set[uuid.UUID] = set()
    wanted_library_ids: set[uuid.UUID] = set()
    for r in rows:
        args = _allowlist_args(r.args)
        started = r.started_at
        seconds = None
        started_ref = None
        if started is not None:
            # procrastinate stores timestamptz; psycopg returns tz-aware.
            started_ref = started if started.tzinfo is not None else started.replace(tzinfo=UTC)
            seconds = max(round((now - started_ref).total_seconds(), 1), 0.0)

        # FIX-6: is the worker driving this job alive, and is the job stalled?
        # worker_alive: the job's worker exists and heartbeat within `hb` seconds.
        # `stalled` mirrors the reaper's detection EXACTLY so the amber UI badge
        # marks precisely the jobs the reaper would act on:
        #   heartbeat net (all jobs): worker_id NULL or heartbeat stale > hb
        #   age net (non-scan only): running longer than `age_limit` seconds
        wh = r.worker_heartbeat
        wh_ref = None
        if wh is not None:
            wh_ref = wh if wh.tzinfo is not None else wh.replace(tzinfo=UTC)
        worker_alive = wh_ref is not None and (now - wh_ref).total_seconds() <= hb
        hb_net = r.worker_id is None or not worker_alive
        age_net = (
            r.task != _SCAN_TASK
            and started_ref is not None
            and (now - started_ref).total_seconds() > age_limit
        )
        stalled = bool(hb_net or age_net)
        item_id = args.get("item_id")
        if isinstance(item_id, str):
            try:
                wanted_item_ids.add(uuid.UUID(item_id))
            except ValueError:
                pass
        lid = args.get("library_id")
        if isinstance(lid, str):
            try:
                wanted_library_ids.add(uuid.UUID(lid))
            except ValueError:
                pass
        jobs.append(
            {
                "id": r.id,
                "queue": r.queue,
                "task": _short_task(r.task),
                "args": args,
                "started_at": started.isoformat() if started else None,
                "seconds_running": seconds,
                "attempts": int(r.attempts) if r.attempts is not None else 0,
                "retry_cap": retry_cap_for(r.task),  # FIX-12: budget for "attempts/cap"
                "worker_id": int(r.worker_id) if r.worker_id is not None else None,
                "worker_alive": worker_alive,
                "stalled": stalled,
                "rel_path": None,  # filled below for resolvable item_ids
                "size": None,  # filled below (free from the same items query)
                "library_name": None,  # filled below for resolvable library_ids
            }
        )

    # Single IN query resolves item_id -> (rel_path, size) for all collected ids
    # at once (no N+1). ``size`` is free from the same row and lets the UI append a
    # file-size suffix on thumbnail jobs (size predicts thumbnail duration).
    if wanted_item_ids:
        res = await session.execute(
            select(Item.id, Item.rel_path, Item.size).where(Item.id.in_(wanted_item_ids))
        )
        info_by_id = {str(iid): (rel, size) for iid, rel, size in res.all()}
        for job in jobs:
            iid = job["args"].get("item_id")
            if isinstance(iid, str) and iid in info_by_id:
                job["rel_path"], job["size"] = info_by_id[iid]

    # Same pattern for library_id -> human-readable library name.
    if wanted_library_ids:
        res = await session.execute(
            select(Library.id, Library.name).where(Library.id.in_(wanted_library_ids))
        )
        name_by_id = {str(lid): name for lid, name in res.all()}
        for job in jobs:
            lid = job["args"].get("library_id")
            if isinstance(lid, str) and lid in name_by_id:
                job["library_name"] = name_by_id[lid]

    return jobs


async def running_scans(session: AsyncSession) -> list[dict]:
    """Scan runs currently ``running``, with their library name + live stats.

    Shape ``[{id, library_id, library_name, rel_path, started_at, stats}]``.
    ``rel_path`` is the scoped-scan subtree (null for a full-library scan);
    ``stats`` is the live progress blob the scan task batches into
    ``ScanRun.stats``.

    ``started_at`` is emitted so the dashboard can derive throughput
    (``stats.seen`` / elapsed) between polls without a second round-trip — the
    same client-side-rate convention ``resources.io``/``net`` already use. The
    SSE endpoint computes its own ``rate`` from the identical inputs
    (``api/scans.py`` ``_snapshot``); both are cumulative averages since the run
    started, not instantaneous rates.
    """
    rows = (
        await session.execute(
            select(ScanRun, Library.name)
            .join(Library, Library.id == ScanRun.library_id)
            .where(ScanRun.status.in_(("running", "stopping")))
            .order_by(ScanRun.started_at.desc())
            .limit(_RUNNING_CAP)
        )
    ).all()
    return [
        {
            "id": str(run.id),
            "library_id": str(run.library_id),
            "library_name": name,
            "rel_path": run.rel_path,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "stats": dict(run.stats or {}),
        }
        for run, name in rows
    ]


# Aggregate-throughput window. Scans are infrequent and `scan_runs` is small, so
# a 30-day window is cheap even without an index on (status, started_at).
_THROUGHPUT_WINDOW_DAYS = 30


async def scan_throughput(session: AsyncSession) -> dict:
    """Aggregate walk throughput across recent COMPLETED scans.

    Shape ``{runs, files, bytes, seconds, files_per_min, bytes_per_s, window_days}``.

    Weighted deliberately: ``SUM(seen) / SUM(walk_seconds)`` — NOT
    ``AVG(files_per_s)``, which would let a 3-file scoped rescan outweigh a
    500k-file full scan. Only ``finished`` runs count; ``failed``/``cancelled``/
    ``stopped`` runs have partial or absent timing and would skew the average.

    Rows written before ``walk_seconds`` existed (and ``scope_missing``
    early-returns) simply lack the key, so ``->>`` yields NULL and the
    ``> 0`` predicate drops them — no backfill needed. Returns zeros rather than
    raising if nothing qualifies, matching ``thumbnail_totals``.
    """
    walk_s = ScanRun.stats["walk_seconds"].astext.cast(Float)
    seen = func.coalesce(ScanRun.stats["seen"].astext.cast(BigInteger), 0)
    size = func.coalesce(ScanRun.stats["bytes_seen"].astext.cast(BigInteger), 0)
    cutoff = datetime.now(UTC) - timedelta(days=_THROUGHPUT_WINDOW_DAYS)
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(seen), 0),
                func.coalesce(func.sum(size), 0),
                func.coalesce(func.sum(walk_s), 0.0),
            ).where(
                ScanRun.status == "finished",
                ScanRun.started_at >= cutoff,
                # NULL (key absent) fails this predicate, so untimed rows drop out.
                walk_s > 0,
            )
        )
    ).one()
    runs, files, total_bytes, seconds = int(row[0]), int(row[1]), int(row[2]), float(row[3])
    return {
        "runs": runs,
        "files": files,
        "bytes": total_bytes,
        "seconds": round(seconds, 2),
        "files_per_min": round(files * 60.0 / seconds, 1) if seconds > 0 else 0.0,
        "bytes_per_s": round(total_bytes / seconds) if seconds > 0 else 0,
        "window_days": _THROUGHPUT_WINDOW_DAYS,
    }


async def jobs_summary(session: AsyncSession) -> dict:
    """One composable snapshot the Jobs dashboard polls.

    Composes existing read-only helpers (no new bookkeeping): the per-queue
    rollup + flat extract summary (:func:`queue_snapshot`), the in-flight list
    (:func:`running_jobs`), the 10 most recent failures (:func:`failed_jobs`),
    the Meili drift snapshot (:func:`meili_snapshot`), and the running scans
    (:func:`running_scans`).

    Shape::

        {
          "queues": {<queue>: {<status>: n, ...}, ...},
          "extract": {"depth", "running", "done", "failed"},
          "running": [<running_jobs>],
          "failed_recent": [<failed_jobs limit 10>],
          "meili": {<meili_snapshot>},
          "scans_running": [<running_scans>],
          "stalled": {"total": n, "by_queue": {<queue>: n, ...}},
          "priorities": {<queue>: <default priority int>, ...},   # UI-T14
          "staged_pipeline": <bool>,                               # UI-T14
          "disk": {"status", "low": [...], "paths": [...]},        # FIX-11 + monitor
          "resources": {"cpu": {...}, "io": {...}|None,            # load monitors
                        "net": {...}|None, "db": {...}|None},
          "thumbs": {"generated", "bytes", "failed_jobs", "queue"},# thumbs monitor
          "scan_throughput": {"runs", "files", "bytes", "seconds", # walk rate
                              "files_per_min", "bytes_per_s", "window_days"},
          "upcoming": {<queue>: [{label, at, task}, ...], ...},    # scheduled work
        }

    ``disk.paths`` is de-duplicated by physical device (co-located watch roles
    collapse to one row); ``disk.low`` stays per watch-role (untouched banner
    contract). ``resources.io``/``resources.net`` are CUMULATIVE /proc counters
    (rates are computed client-side between polls); both are ``None`` off Linux.
    ``resources.db`` is a cheap Postgres health snapshot (``None`` on any failure).
    ``upcoming`` lists at most 3 soonest scheduled jobs per queue (procrastinate
    ``scheduled_at`` rows + cron-derived next scan/report occurrences).
    """
    settings = get_settings()
    queues = await queue_snapshot(session)
    running = await running_jobs(session)
    failed_recent = await failed_jobs(session, limit=10)
    meili = await meili_snapshot(session)
    scans = await running_scans(session)
    thumb_totals = await thumbnail_totals(session)
    throughput = await scan_throughput(session)

    # UI-T14: expose the per-task-class default priorities (so the Jobs page shows
    # each queue's current default in its stepper) and whether the staged pipeline
    # is on (so the extract card can render the "waiting for scan" hint while a scan
    # is running). Read-only config echo -- no query. Every queue the worker
    # registers is listed so its card renders a working stepper (thumbs/exports
    # were previously missing, so their cards had no priority control).
    priorities = {
        "scan": settings.scan_priority,
        "extract": settings.extract_priority,
        "index": settings.index_priority,
        "embed": settings.embed_priority,
        "maintenance": settings.maintenance_priority,
        "alerts": settings.alerts_priority,
        "thumbs": settings.thumbs_priority,
        "exports": settings.exports_priority,
    }

    # FIX-6: roll up the stalled flag the in-flight list already computed into a
    # cheap {total, by_queue} summary the dashboard renders as amber badges. This
    # is derived from `running` (no extra query); it counts only jobs the reaper
    # would act on this instant.
    by_queue: dict[str, int] = {}
    for job in running:
        if job.get("stalled"):
            by_queue[job["queue"]] = by_queue.get(job["queue"], 0) + 1
    stalled = {"total": sum(by_queue.values()), "by_queue": by_queue}

    # FIX-11 + monitor: piggyback the disk-headroom rollup AND the coarse CPU-load
    # indicator on the Jobs dashboard's existing 4s poll so neither needs an extra
    # request. os.statvfs on a couple of paths + os.getloadavg + /proc counters,
    # offloaded together so a slow mount never blocks the event loop and the
    # statvfs pass runs ONCE.
    from starlette.concurrency import run_in_threadpool

    disk, resources = await run_in_threadpool(_disk_and_resources)

    # Cheap Postgres health tile from the SAME session (a few catalog reads); the
    # queue backlog reuses the already-fetched snapshot (no extra query). Wrapped so
    # ANY failure (permissions / odd PG) degrades to null rather than breaking the
    # summary. Upcoming scheduled work rides the same poll (one procrastinate query
    # + cheap cron projections over the already-loaded schedule rows).
    queue_backlog = sum(q.get("todo", 0) for q in queues["queues"].values())
    resources["db"] = await _db_health(session, queue_backlog)
    upcoming = await _upcoming(session)

    # Thumbnail-creation monitor: the thumbs queue is configurable, so the UI can't
    # guess its name -- re-expose its per-status counts under a stable key, plus the
    # whole-cache generated/bytes aggregate (single source of truth with /stats) and
    # the queue's failed count from the same snapshot. One cheap SELECT.
    thumbs_queue = queues["queues"].get(settings.queue_thumbnail, {})
    thumbs = {
        "generated": thumb_totals["count"],
        "bytes": thumb_totals["bytes"],
        "failed_jobs": int(thumbs_queue.get("failed", 0)),
        "queue": thumbs_queue,
    }

    return {
        "queues": queues["queues"],
        "extract": queues["extract"],
        "running": running,
        "failed_recent": failed_recent,
        "meili": meili,
        "scans_running": scans,
        "stalled": stalled,
        "priorities": priorities,
        "staged_pipeline": settings.staged_pipeline,
        "disk": disk,
        "resources": resources,
        "thumbs": thumbs,
        "scan_throughput": throughput,
        "upcoming": upcoming,
    }


async def thumbnail_totals(session: AsyncSession) -> dict:
    """Whole-cache thumbnail aggregate ``{count, bytes, by_source}`` (single
    source of truth for both ``/stats`` and the Jobs ``thumbs`` monitor).

    One grouped aggregate over the disposable ``thumbnail_manifest`` projection --
    no filesystem walk. Guards on table existence with ``to_regclass`` so a fresh
    DB (before ``init_db`` created the app tables) returns zeros rather than
    raising, keeping the Jobs summary total on a bare database."""
    exists = (
        await session.execute(text("SELECT to_regclass('thumbnail_manifest')"))
    ).scalar()
    if exists is None:
        return {"count": 0, "bytes": 0, "by_source": {}}

    from filearr.models import ThumbnailManifest

    rows = (
        await session.execute(
            select(
                ThumbnailManifest.source,
                func.count(),
                func.coalesce(func.sum(ThumbnailManifest.bytes), 0),
            ).group_by(ThumbnailManifest.source)
        )
    ).all()
    by_source: dict[str, dict] = {}
    total_count = 0
    total_bytes = 0
    for source, count, byts in rows:
        by_source[source] = {"count": int(count), "bytes": int(byts)}
        total_count += int(count)
        total_bytes += int(byts)
    return {"count": total_count, "bytes": total_bytes, "by_source": by_source}


def _disk_and_resources() -> tuple[dict, dict]:
    """Combined FIX-11 disk rollup + coarse resource indicators, computed in ONE
    threadpool offload (a single ``os.statvfs`` pass + ``os.getloadavg`` + two
    ``/proc`` reads) so a slow mount never blocks the event loop.

    ``disk`` keeps the untouched banner contract ``{status, low}`` (``low`` lists
    only the non-ok watch paths, per watch-role) and ADDS ``paths`` -- the full
    per-DEVICE detail (co-located watch roles collapsed by ``st_dev`` via
    :func:`diskguard.dedupe_by_device`) the always-on space indicator renders.
    ``resources`` carries :func:`_cpu_load`, plus CUMULATIVE ``io``/``net``
    counters (``None`` off Linux). The ``db`` health tile is filled by the async
    caller (needs the DB session)."""
    from filearr import diskguard

    settings = get_settings()
    statuses = diskguard.monitored_statuses(settings)
    merged = diskguard.dedupe_by_device(statuses)
    paths = [
        {
            "label": m["label"],
            "path": m["path"],
            "total": m["total"],
            "free": m["free"],
            "used": m["used"],
            "pct_free": round(m["pct_free"], 2),
            "status": m["status"],
            "reason": m["reason"],
            "is_pg": m["is_pg"],
            "members": m["members"],
        }
        for m in merged
    ]
    low = [
        {
            "label": st.get("label", st["path"]),
            "path": st["path"],
            "free": st["free"],
            "total": st["total"],
            "pct_free": round(st["pct_free"], 2),
            "status": st["status"],
            "reason": st["reason"],
        }
        for st in statuses
        if st["status"] != diskguard.OK
    ]
    disk = {"status": diskguard.overall_status(statuses), "low": low, "paths": paths}
    return disk, {"cpu": _cpu_load(), "io": _diskstats_bytes(), "net": _net_bytes()}


# --- /proc I/O + network counters (cumulative; rates computed client-side) --- #

# Whole physical block devices only, so summing does not double-count a device
# and its partitions (sda + sda1), and skips loop/ram/dm/zram/md pseudo-devices.
# sd*/vd*/xvd*/hd* whole disks end in a letter (partitions add a digit); nvme
# whole namespaces are ``nvmeXnY`` (partitions add ``pZ``); ``mmcblkN`` (partitions
# add ``pZ``).
_WHOLE_DISK_RE = re.compile(r"(?:(?:sd|vd|xvd|hd)[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def _diskstats_bytes(text: str | None = None) -> dict | None:
    """Cumulative bytes read/written across physical disks from ``/proc/diskstats``.

    Returns ``{"read_bytes", "write_bytes"}`` (sectors × 512), or ``None`` off
    Linux / when the file is unreadable (the UI hides the tile). Only WHOLE block
    devices are summed (:data:`_WHOLE_DISK_RE`) so a device and its partitions are
    not double-counted and loop/ram/dm pseudo-devices are ignored. ``text`` may be
    supplied for tests (mirrors ``schedule.is_network_path``)."""
    if text is None:
        try:
            with open("/proc/diskstats", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
    read_sectors = 0
    write_sectors = 0
    for line in text.splitlines():
        parts = line.split()
        # Layout: major minor name reads rd_merged rd_sectors ... writes wr_merged
        # wr_sectors ...  (name at index 2, rd_sectors at 5, wr_sectors at 9).
        if len(parts) < 10:
            continue
        name = parts[2]
        if not _WHOLE_DISK_RE.fullmatch(name):
            continue
        try:
            read_sectors += int(parts[5])
            write_sectors += int(parts[9])
        except ValueError:
            continue
    return {"read_bytes": read_sectors * 512, "write_bytes": write_sectors * 512}


def _net_bytes(text: str | None = None) -> dict | None:
    """Cumulative rx/tx bytes across all interfaces except ``lo`` from
    ``/proc/net/dev``.

    Returns ``{"rx_bytes", "tx_bytes"}``, or ``None`` off Linux / when the file is
    unreadable. ``text`` may be supplied for tests."""
    if text is None:
        try:
            with open("/proc/net/dev", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
    rx = 0
    tx = 0
    for line in text.splitlines():
        if ":" not in line:
            continue  # skip the two header rows
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        if iface == "lo" or not iface:
            continue
        fields = rest.split()
        # rx_bytes is the first field after the colon; tx_bytes is the 9th.
        if len(fields) < 9:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
    return {"rx_bytes": rx, "tx_bytes": tx}


# --- Postgres health tile (cheap catalog reads; total on any failure) -------- #

_DB_ACTIVITY_SQL = """
SELECT
    count(*)                                                         AS backends,
    count(*) FILTER (WHERE state = 'active')                        AS active,
    count(*) FILTER (WHERE state = 'idle in transaction')           AS idle_in_tx,
    count(*) FILTER (WHERE wait_event IS NOT NULL AND state = 'active') AS waiting,
    COALESCE(EXTRACT(EPOCH FROM max(
        CASE WHEN state = 'active' THEN now() - query_start END)), 0) AS longest_query_s,
    COALESCE(EXTRACT(EPOCH FROM max(
        CASE WHEN state = 'idle in transaction' THEN now() - state_change END)), 0)
                                                                    AS longest_idle_in_tx_s
FROM pg_stat_activity
WHERE datname = current_database()
"""

_DB_STATS_SQL = """
SELECT blks_hit, blks_read, deadlocks, temp_files, temp_bytes,
       xact_commit, xact_rollback
FROM pg_stat_database
WHERE datname = current_database()
"""


async def _db_health(session: AsyncSession, queue_backlog: int) -> dict | None:
    """Cheap Postgres health snapshot for the Jobs dashboard's DB tile.

    Two aggregate catalog reads (``pg_stat_activity`` + ``pg_stat_database`` for
    the current database) — NO ``pg_stat_statements``, NO long-running scans. Any
    failure (permissions, an odd PG build, a bare/queue-only test DB) returns
    ``None`` so the summary is never broken by the tile. ``queue_backlog`` is the
    already-computed total procrastinate ``todo`` count (no extra query)."""
    try:
        act = (await session.execute(text(_DB_ACTIVITY_SQL))).mappings().first()
        dbs = (await session.execute(text(_DB_STATS_SQL))).mappings().first()
    except Exception:  # noqa: BLE001 - a monitoring tile must never break the poll
        return None
    if act is None or dbs is None:
        return None
    hit = int(dbs["blks_hit"] or 0)
    read = int(dbs["blks_read"] or 0)
    denom = hit + read
    cache_hit_ratio = round(hit / denom, 4) if denom > 0 else None
    return {
        "backends": int(act["backends"] or 0),
        "active": int(act["active"] or 0),
        "idle_in_tx": int(act["idle_in_tx"] or 0),
        "waiting": int(act["waiting"] or 0),
        "longest_query_s": round(float(act["longest_query_s"] or 0), 1),
        "longest_idle_in_tx_s": round(float(act["longest_idle_in_tx_s"] or 0), 1),
        "cache_hit_ratio": cache_hit_ratio,
        "deadlocks": int(dbs["deadlocks"] or 0),
        "temp_files": int(dbs["temp_files"] or 0),
        "temp_bytes": int(dbs["temp_bytes"] or 0),
        "xact_commit": int(dbs["xact_commit"] or 0),
        "xact_rollback": int(dbs["xact_rollback"] or 0),
        "queue_backlog": int(queue_backlog),
    }


# --- upcoming scheduled work (per queue, soonest 3) -------------------------- #

_UPCOMING_SCHEDULED_SQL = """
SELECT queue_name, task_name, scheduled_at
FROM (
    SELECT queue_name, task_name, scheduled_at,
           row_number() OVER (PARTITION BY queue_name ORDER BY scheduled_at) AS rn
    FROM procrastinate_jobs
    WHERE status = 'todo'
      AND scheduled_at IS NOT NULL
      AND scheduled_at > now()
) t
WHERE rn <= 3
ORDER BY queue_name, scheduled_at
"""


async def _upcoming(session: AsyncSession) -> dict:
    """Per-queue list of the soonest 3 upcoming scheduled jobs (``{queue: [{label,
    at, task}]}``, soonest first).

    Three cheap sources, all single round-trips:

      * procrastinate ``todo`` rows with a future ``scheduled_at`` (row-numbered so
        the DB caps at 3/queue at the source),
      * cron-derived next occurrences of every enabled, non-agent library's
        ``scan_cron`` + enabled ``scan_paths`` cron → the ``scan`` queue,
      * enabled ``report_schedules`` next occurrences → the ``exports`` queue.

    Cron next-times reuse :func:`schedule.next_occurrence` (the same cronsim engine
    the scheduler evaluates). Any source that raises (bare DB, missing table) is
    skipped so the projection is total. Cron/report merges are re-capped to 3."""
    from filearr import schedule
    from filearr.models import Library, ReportSchedule, ScanPath

    now = datetime.now(UTC)
    upcoming: dict[str, list[dict]] = {}

    def _add(queue: str, label: str, at: datetime, task: str) -> None:
        upcoming.setdefault(queue, []).append(
            {"label": label, "at": at.isoformat(), "task": task}
        )

    # 1) procrastinate scheduled_at rows (capped 3/queue at the source).
    try:
        exists = (
            await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
        ).scalar()
        if exists is not None:
            rows = (await session.execute(text(_UPCOMING_SCHEDULED_SQL))).all()
            for queue_name, task_name, scheduled_at in rows:
                at = scheduled_at
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                _add(queue_name, _short_task(task_name), at, _short_task(task_name))
    except Exception:  # noqa: BLE001 - projection must stay total
        pass

    # 2) cron-derived next scans (libraries + hot folders) -> the `scan` queue.
    try:
        libs = list(
            (
                await session.execute(
                    select(Library.id, Library.name, Library.scan_cron).where(
                        Library.enabled.is_(True),
                        Library.source_agent_id.is_(None),
                    )
                )
            ).all()
        )
        lib_name = {lid: name for lid, name, _ in libs}
        for _lid, name, cron in libs:
            if not cron:
                continue
            nxt = schedule.next_occurrence(cron, now)
            if nxt is not None:
                _add("scan", name, nxt, "scan_library")
        sp_rows = list(
            (
                await session.execute(
                    select(ScanPath.library_id, ScanPath.rel_path, ScanPath.scan_cron)
                    .join(Library, Library.id == ScanPath.library_id)
                    .where(
                        ScanPath.enabled.is_(True),
                        ScanPath.scan_cron.isnot(None),
                        Library.enabled.is_(True),
                        Library.source_agent_id.is_(None),
                    )
                )
            ).all()
        )
        for lid, rel_path, cron in sp_rows:
            nxt = schedule.next_occurrence(cron, now)
            if nxt is None:
                continue
            label = rel_path or lib_name.get(lid, "root")
            _add("scan", label, nxt, "scan_library")
    except Exception:  # noqa: BLE001 - projection must stay total
        pass

    # 3) enabled report schedules -> the `exports` queue.
    try:
        sched_rows = list(
            (
                await session.execute(
                    select(ReportSchedule.name, ReportSchedule.cron).where(
                        ReportSchedule.enabled.is_(True)
                    )
                )
            ).all()
        )
        for name, cron in sched_rows:
            nxt = schedule.next_occurrence(cron, now)
            if nxt is not None:
                _add("exports", name, nxt, "run_report_export")
    except Exception:  # noqa: BLE001 - projection must stay total
        pass

    # Sort each queue soonest-first and cap at 3 (the cron/report merges may have
    # pushed scan/exports past 3).
    for queue in list(upcoming):
        upcoming[queue].sort(key=lambda e: e["at"])
        upcoming[queue] = upcoming[queue][:3]
    return upcoming


def _cpu_load() -> dict:
    """Coarse CPU-load indicator riding the Jobs poll -- NOT a metrics system.

    ``os.getloadavg`` is the Unix 1/5/15-minute run-queue average; the product
    runs in Linux containers where it is the cheapest honest load signal. On a
    host without it (Windows dev boxes -> AttributeError; some restricted
    environments -> OSError) every field degrades to ``None`` and the caller never
    crashes. ``cores`` prefers the process's CPU affinity (the cores actually
    schedulable) and falls back to the machine count. ``percent`` is
    ``100 * load1 / cores`` -- it MAY exceed 100 when the run queue is deeper than
    the core count; that overload is honest, so it is not clamped here (the UI
    clamps only the bar width, never the number)."""
    load1 = load5 = load15 = None
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        pass

    cores: int | None
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count()

    percent = None
    if load1 is not None and cores:
        percent = round(100 * load1 / cores, 1)

    return {
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cores": cores,
        "percent": percent,
    }
