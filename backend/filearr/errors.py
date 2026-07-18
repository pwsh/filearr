"""Error-surfacing helpers (T11).

Deliberately-corrupt or unreadable files must show up as *visible* counts, not
silence. Extractors record a ``_extract_error`` key inside ``Item.metadata_``
(T1 convention); this module turns those into cheap, read-only aggregates and a
paginated failing-items listing, plus a read-only view of recently *failed*
Procrastinate jobs.

Security note: every error string here is UNTRUSTED — it is derived from file
names and third-party parser exceptions (guessit/ffprobe/pypdf/trimesh/...),
which may embed attacker-controlled bytes from a crafted filename. Before any
error string crosses the API boundary it is passed through
:func:`sanitize_error`: control characters are stripped (log/terminal/UI
injection defense) and the result is truncated to :data:`MAX_ERROR_CHARS`. The
same cap is applied at *store* time (scan crash handler, per-run counter) so a
pathological message can never bloat a JSONB row.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Storage/return cap for a single error string. Applied both when persisting
# (ScanRun.stats.error) and when returning parser errors via the API.
MAX_ERROR_CHARS = 500


# --- FIX-12: per-task genuine-failure retry budget -------------------------
# The Jobs page shows procrastinate's raw per-job ``attempts`` counter. That
# number is honest but easily MISREAD: for ``extract_item`` it also counts the
# staged-pipeline gate *reschedules* (a wait while a scan walks the same library
# is a ``retry_job`` with ZERO work done — procrastinate 3.9 has a single
# ``attempts`` column and no separate reschedule counter, see
# ``tasks/extract.StagedExtractRetry``), so a single file can show attempts in
# the dozens/hundreds during a big scan without ever having FAILED. Pre-FIX-8
# reaper requeues inflated it the same way (the live box saw attempts=50/51).
#
# To de-alarm the number we surface, for each job, the task's genuine-failure
# retry budget (``retry_cap``) so the UI can render ``attempts/cap`` and show any
# excess separately (reschedules/requeues, not failures). Keyed by the FULLY-
# QUALIFIED task name; ``None`` (task absent from the map) means the task carries
# NO retry, so a single failure is terminal (every periodic maintenance tick).
#
# These MIRROR the retry strategies configured on the task decorators; a unit
# test (test_attempts_fix12) asserts they stay in lock-step with the authoritative
# source constants so a budget change here can never silently drift.
TASK_RETRY_CAPS: dict[str, int] = {
    "filearr.tasks.extract.extract_item": 2,       # extract.EXTRACT_MAX_ATTEMPTS
    "filearr.tasks.index_sync.sync_items": 4,       # retrying.MEILI_RETRY_MAX_ATTEMPTS
    "filearr.tasks.index_sync.rebuild_index": 4,    # retrying.MEILI_RETRY_MAX_ATTEMPTS
    "filearr.tasks.embed.embed_item": 2,            # embed.py retry=2
    "filearr.tasks.embed.embed_missing": 2,         # embed.py retry=2
}


def retry_cap_for(task_name: str | None) -> int | None:
    """Genuine-failure retry budget for a fully-qualified task name (FIX-12).

    ``None`` when the task carries no retry (a single failure is terminal) or the
    name is unknown, so callers render just the raw attempts."""
    if not task_name:
        return None
    return TASK_RETRY_CAPS.get(task_name)


def sanitize_error(value: object, *, limit: int = MAX_ERROR_CHARS) -> str:
    """Make an untrusted error string safe to store and to return via the API.

    * Coerce to ``str`` (parser exceptions may be arbitrary objects).
    * Drop C0/C1 control characters (0x00–0x1F, 0x7F–0x9F) EXCEPT that tabs and
      newlines collapse to a single space — this neutralises ANSI escape
      sequences, NUL bytes and CR/LF log-injection while keeping the message
      human-readable.
    * Truncate to ``limit`` characters (an ellipsis marks truncation).
    """
    s = str(value)
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if ch in ("\t", "\n", "\r"):
            out.append(" ")
        elif o < 0x20 or 0x7F <= o <= 0x9F:
            continue  # strip other control chars (ANSI/NUL/etc.)
        else:
            out.append(ch)
    cleaned = "".join(out).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


async def extract_error_count(session: AsyncSession, library_id: str) -> int:
    """Count active items in a library whose metadata carries ``_extract_error``.

    Single aggregate query using the JSONB key-exists (``?``) operator, which the
    existing ``ix_items_metadata`` GIN index (``jsonb_ops``) can serve — no
    per-row scan. This is the AUTHORITATIVE, always-correct error count
    (extract jobs run asynchronously after a scan, so a live re-count beats any
    counter that could drift). ``?`` must be escaped as ``??`` for the DBAPI
    param substitution.
    """
    row = await session.execute(
        text(
            "SELECT count(*) FROM items "
            "WHERE library_id = :lib AND status = 'active' "
            "AND metadata ? '_extract_error'"
        ),
        {"lib": str(library_id)},
    )
    return int(row.scalar() or 0)


async def extract_error_counts_by_library(session: AsyncSession) -> dict[str, int]:
    """Library_id -> extract-error count for every library that has any.

    One grouped aggregate over the GIN-indexed predicate; libraries with zero
    errors are simply absent from the map (callers default to 0).
    """
    rows = await session.execute(
        text(
            "SELECT library_id::text AS lib, count(*) AS n FROM items "
            "WHERE status = 'active' AND metadata ? '_extract_error' "
            "GROUP BY library_id"
        )
    )
    return {lib: int(n) for lib, n in rows.all()}


async def failing_items(
    session: AsyncSession, library_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Paginated list of active items in a library that failed extraction.

    Returns ``[{id, rel_path, error}]`` ordered by rel_path (stable pagination).
    The ``error`` string is sanitized before it leaves the DB layer. ``limit`` is
    capped by the caller (API) to a small page size.
    """
    rows = await session.execute(
        text(
            "SELECT id::text AS id, rel_path, metadata ->> '_extract_error' AS err "
            "FROM items "
            "WHERE library_id = :lib AND status = 'active' "
            "AND metadata ? '_extract_error' "
            "ORDER BY rel_path LIMIT :limit OFFSET :offset"
        ),
        {"lib": str(library_id), "limit": limit, "offset": offset},
    )
    return [
        {"id": r.id, "rel_path": r.rel_path, "error": sanitize_error(r.err)}
        for r in rows.all()
    ]


# NOTE on error snippets: procrastinate 3.9's ``procrastinate_events`` table
# stores only (job_id, type, at) -- it does NOT persist the exception text/
# traceback of a failed job (that goes to the worker's logs, not the DB). So the
# richest DB-side signal we can surface per failed job is *when* the last event
# fired. We expose that as ``attempted_at`` and set ``error`` to None (kept in
# the shape for forward-compat + honesty). If a future procrastinate adds an
# error column this is the one query to extend. The item-level ``_extract_error``
# path (extract_error_count / failing_items) is where the actual parser messages
# live and are surfaced.
async def failed_jobs(session: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[dict]:
    """Recent failed Procrastinate jobs (read-only, capped).

    Shape ``[{id, queue, task, status, attempts, retry_cap, scheduled_at,
    attempted_at, error}]``. ``retry_cap`` is the task's genuine-failure retry
    budget (FIX-12) so the UI can render ``attempts/cap``; null when the task
    carries no retry. ``attempted_at`` is the timestamp of the job's most recent event
    (best proxy for "when it last failed"); ``error`` is always None because
    procrastinate 3.9 does not store per-job error text in the DB (see module
    note). Returns ``[]`` when the procrastinate schema is absent so the endpoint
    stays total on a fresh DB.
    """
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return []

    events_exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_events')"))
    ).scalar()
    attempted_expr = (
        "(SELECT max(e.at) FROM procrastinate_events e WHERE e.job_id = j.id)"
        if events_exists is not None
        else "NULL"
    )

    rows = await session.execute(
        text(
            "SELECT j.id::text AS id, j.queue_name AS queue, j.task_name AS task, "
            "       j.status::text AS status, j.attempts AS attempts, "
            "       j.scheduled_at AS scheduled_at, "
            f"      {attempted_expr} AS attempted_at "
            "FROM procrastinate_jobs j "
            "WHERE j.status = 'failed' "
            "ORDER BY j.id DESC LIMIT :limit OFFSET :offset"
        ),
        {"limit": limit, "offset": offset},
    )
    return [
        {
            "id": r.id,
            "queue": r.queue,
            "task": r.task,
            "status": r.status,
            "attempts": int(r.attempts) if r.attempts is not None else None,
            "retry_cap": retry_cap_for(r.task),  # FIX-12: budget for "attempts/cap"
            "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            "attempted_at": r.attempted_at.isoformat() if r.attempted_at else None,
            "error": None,
        }
        for r in rows.all()
    ]


async def failed_jobs_count(session: AsyncSession, *, queue: str | None = None) -> int:
    """Total number of ``failed`` Procrastinate jobs (FIX-8).

    Backs the ``total`` field of the paginated ``/system/failed-jobs`` response so
    the UI can render a real pager (page N of M) instead of guessing whether more
    rows exist. Optional ``queue`` filter mirrors the manual-clear endpoint. One
    cheap aggregate over ``procrastinate_jobs``; returns 0 when the procrastinate
    schema is absent (fresh DB) so the endpoint stays total.
    """
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return 0
    sql = "SELECT count(*) FROM procrastinate_jobs WHERE status = 'failed'"
    params: dict = {}
    if queue is not None:
        sql += " AND queue_name = :queue"
        params["queue"] = queue
    row = await session.execute(text(sql), params)
    return int(row.scalar() or 0)
