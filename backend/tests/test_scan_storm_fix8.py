"""FIX-8 (scan-scheduling storm): once-per-occurrence cron, scan-job dedupe,
reaper ScanRun-failed transition + bounded reaper requeue, and no-retry periodic
tasks.

Live bug (2026-07-13): a worker OOMed mid-scan BEFORE its ScanRun row committed,
so neither the running-ScanRun guard nor the partial (``todo``-only)
queueing_lock saw the stalled ``doing`` job -- every due tick re-deferred,
stacking 5-6 duplicate scan jobs per library, and the reaper requeued other
stalled jobs unboundedly (attempts=50/51).

Three layers, mirroring the reaper test's real-Procrastinate-on-pgserver style:
  * pure ``due_occurrence`` unit matrix (no DB);
  * retry-policy assertions over the registered task objects;
  * a proc_app bound to a throwaway PG for ``scan_job_pending`` dedupe + the
    reaper's ScanRun-failed transition + the requeue cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCAN_TASK = "filearr.tasks.scan.scan_library"
EXTRACT_TASK = "filearr.tasks.extract.extract_item"


def _t(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# due_occurrence — once-per-occurrence matrix (pure, no DB)                    #
# --------------------------------------------------------------------------- #
def test_first_fire_only_at_exact_minute_no_backfill():
    from filearr.schedule import due_occurrence

    # last_fired=None (never fired): fires only when NOW is the occurrence minute,
    # never a backfilled catch-up of an earlier same-day occurrence.
    assert due_occurrence("0 4 * * *", _t(2026, 7, 7, 4, 0, 30), None) == _t(2026, 7, 7, 4, 0)
    assert due_occurrence("0 4 * * *", _t(2026, 7, 7, 15, 0, 0), None) is None


def test_occurrence_fires_once_then_not_again_that_occurrence():
    from filearr.schedule import due_occurrence

    lf = _t(2026, 7, 7, 4, 0)
    # Same occurrence, later tick within the minute / next minutes -> not re-due.
    assert due_occurrence("0 4 * * *", _t(2026, 7, 7, 4, 0, 59), lf) is None
    assert due_occurrence("0 4 * * *", _t(2026, 7, 7, 4, 30, 0), lf) is None
    # Next day's occurrence IS due.
    assert due_occurrence("0 4 * * *", _t(2026, 7, 8, 4, 0, 0), lf) == _t(2026, 7, 8, 4, 0)


def test_every_five_minutes_fires_once_per_slot_not_per_tick():
    from filearr.schedule import due_occurrence

    lf = _t(2026, 7, 7, 14, 0)
    # The storm cadence: minute ticks between slots do NOT re-fire the 14:00 slot.
    for m in (1, 2, 3, 4):
        assert due_occurrence("*/5 * * * *", _t(2026, 7, 7, 14, m, 0), lf) is None
    # The next 5-min slot fires exactly once.
    assert due_occurrence("*/5 * * * *", _t(2026, 7, 7, 14, 5, 0), lf) == _t(2026, 7, 7, 14, 5)


def test_missed_occurrences_collapse_to_latest_no_per_slot_backfill():
    from filearr.schedule import due_occurrence

    # Scheduler was down; last consumed = 07-07 04:00, now = 07-10 04:00. Only the
    # single LATEST occurrence fires (one catch-up scan, not one per missed day).
    got = due_occurrence("0 4 * * *", _t(2026, 7, 10, 4, 0, 0), _t(2026, 7, 7, 4, 0))
    assert got == _t(2026, 7, 10, 4, 0)


def test_catchup_window_bounds_how_far_back_we_fire():
    from filearr.schedule import due_occurrence

    # A weekly occurrence 5 days stale is OUTSIDE the default 48h catch-up window
    # for a tick that is not itself an occurrence -> nothing fires (waits for next).
    got = due_occurrence(
        "0 4 * * 0",  # Sundays 04:00; 2026-07-05 is a Sunday
        _t(2026, 7, 10, 12, 0, 0),  # Friday noon, not an occurrence
        _t(2026, 7, 5, 4, 0),
        max_catchup_minutes=2880,
    )
    assert got is None


def test_crash_between_semantics_at_most_once():
    from filearr.schedule import due_occurrence

    # Simulate: occurrence stamped as fired (last_fired = the occurrence) but the
    # enqueue crashed. Next tick must NOT re-fire the SAME occurrence (at-most-once
    # beats storming) -- only a strictly-newer occurrence fires.
    occ = _t(2026, 7, 7, 4, 0)
    assert due_occurrence("0 4 * * *", _t(2026, 7, 7, 4, 1, 0), occ) is None


def test_disabled_and_invalid_cron_never_due():
    from filearr.schedule import due_occurrence

    for expr in ("", "   ", None):
        assert due_occurrence(expr, _t(2026, 7, 7, 4, 0), None) is None  # type: ignore[arg-type]
    assert due_occurrence("garbage expr", _t(2026, 7, 7, 4, 0), None) is None
    assert due_occurrence("* * * *", _t(2026, 7, 7, 4, 0), None) is None  # too few fields


def test_naive_last_fired_is_tolerated():
    from filearr.schedule import due_occurrence

    # last_cron_fired_at may arrive naive from some drivers; comparison is UTC-safe.
    got = due_occurrence("0 4 * * *", _t(2026, 7, 8, 4, 0, 0), datetime(2026, 7, 7, 4, 0))
    assert got == _t(2026, 7, 8, 4, 0)


# --------------------------------------------------------------------------- #
# retry-policy assertions over the registered task objects                    #
# --------------------------------------------------------------------------- #
def test_periodic_tasks_have_no_retry():
    """Every periodic maintenance/scheduler task must carry NO retry strategy:
    the next tick re-runs it, and self-retry here was a runaway-attempts source."""
    import filearr.tasks.thumbs as thumbs
    import filearr.worker as w

    for task in (
        w.schedule_scans,
        w.reap_stalled_jobs,
        w.purge_recycle_bin,
        w.purge_item_versions,
        w.purge_alert_events,
        w.purge_job_history,
        w.nightly_reconcile,
        w.reconcile_meili,
        w.reap_shadow_indexes,
        thumbs.gc_thumbnails,
    ):
        assert task.retry_strategy is None, task.name


def test_scan_library_has_no_retry():
    """scan_library must not self-retry (the reaper fails it; operator retriggers)."""
    import filearr.tasks.scan as sc

    assert sc.scan_library.retry_strategy is None


def test_sync_items_and_extract_have_bounded_retry():
    """The projection/extract tasks keep a BOUNDED retry with a backoff cap."""
    import filearr.tasks.extract as ex
    import filearr.tasks.index_sync as ix
    from filearr.retrying import ExponentialRetry

    assert isinstance(ix.sync_items.retry_strategy, ExponentialRetry)
    assert ix.sync_items.retry_strategy.max_attempts == 4
    # backoff cap present (FIX-8): wait is clamped, never unbounded.
    assert ix.sync_items.retry_strategy.max_wait_seconds is not None
    assert ix.sync_items.retry_strategy.wait_seconds(100) == pytest.approx(
        ix.sync_items.retry_strategy.max_wait_seconds
    )
    # extract has its own bounded strategy (staged reschedule + <=2 real retries).
    assert ex.extract_item.retry_strategy is not None
    assert ex.EXTRACT_MAX_ATTEMPTS == 2


# --------------------------------------------------------------------------- #
# proc_app-bound: dedupe + reaper ScanRun transition + requeue cap            #
# --------------------------------------------------------------------------- #
procrastinate = pytest.importorskip("procrastinate")

pytestmark_async = pytest.mark.asyncio


@pytest.fixture(scope="module")
def storm_pg(module_db):
    # module_db already carries the uuidv7() shim; add the minimal scan_runs table
    # for the reaper's ScanRun-failed UPDATE (the real column set it touches). No
    # libraries FK needed for an UPDATE.
    import psycopg

    with psycopg.connect(module_db.get_uri(), autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scan_runs ("
            "  id uuid PRIMARY KEY DEFAULT uuidv7(),"
            "  library_id uuid NOT NULL,"
            "  started_at timestamptz NOT NULL DEFAULT now(),"
            "  finished_at timestamptz,"
            "  status text NOT NULL DEFAULT 'running',"
            "  rel_path text,"
            "  stats jsonb NOT NULL DEFAULT '{}'::jsonb"
            ");"
        )
    return module_db


@pytest.fixture
async def proc_app_storm(storm_pg):
    import psycopg
    from procrastinate import PsycopgConnector

    from filearr.worker import proc_app

    dsn = storm_pg.get_uri()
    connector = PsycopgConnector(conninfo=dsn)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "TRUNCATE procrastinate_jobs, procrastinate_events, "
                    "procrastinate_workers RESTART IDENTITY CASCADE"
                )
                conn.execute("TRUNCATE scan_runs")
            yield storm_pg
    proc_app.connector = original


def _conn(pg):
    import psycopg

    return psycopg.connect(pg.get_uri(), autocommit=True)


def _insert_scan_job(conn, *, library_id, rel_path=None, status="doing", worker_id=None,
                     attempts=0) -> int:
    import json

    args = {"library_id": library_id}
    if rel_path is not None:
        args["rel_path"] = rel_path
    return conn.execute(
        "INSERT INTO procrastinate_jobs "
        "(queue_name, task_name, args, status, worker_id, attempts) "
        "VALUES ('scan', %s, %s::jsonb, %s::procrastinate_job_status, %s, %s) RETURNING id",
        (SCAN_TASK, json.dumps(args), status, worker_id, attempts),
    ).fetchone()[0]


def _job_status(conn, jid) -> str:
    return conn.execute(
        "SELECT status::text FROM procrastinate_jobs WHERE id=%s", (jid,)
    ).fetchone()[0]


@pytestmark_async
async def test_scan_job_pending_detects_unfinished_states(proc_app_storm):
    """scan_job_pending sees todo/doing/aborting scan jobs, ignores terminal."""
    from filearr.worker import scan_job_pending

    lib = "11111111-1111-1111-1111-111111111111"
    with _conn(proc_app_storm) as conn:
        for st in ("todo", "doing", "aborting"):
            _insert_scan_job(conn, library_id=lib, status=st)
    assert await scan_job_pending(lib, None) is True

    other = "22222222-2222-2222-2222-222222222222"
    assert await scan_job_pending(other, None) is False  # no job for this library

    with _conn(proc_app_storm) as conn:
        _insert_scan_job(conn, library_id=other, status="failed")  # terminal
    assert await scan_job_pending(other, None) is False  # terminal never blocks


@pytestmark_async
async def test_scan_job_pending_full_vs_scoped_are_distinct(proc_app_storm):
    """A scoped (rel_path) unfinished job must NOT dedupe a full-library scan and
    vice-versa (they run independently)."""
    from filearr.worker import scan_job_pending

    lib = "33333333-3333-3333-3333-333333333333"
    with _conn(proc_app_storm) as conn:
        _insert_scan_job(conn, library_id=lib, rel_path="Downloads", status="doing")
    assert await scan_job_pending(lib, "Downloads") is True   # same scope -> pending
    assert await scan_job_pending(lib, None) is False          # full scan not blocked
    assert await scan_job_pending(lib, "Other") is False       # different subtree


@pytestmark_async
async def test_reaper_fails_scanrun_of_stalled_scan(proc_app_storm):
    """A stalled scan_library orphan is FAILED and its running ScanRun is
    transitioned running->failed with a reaped note (invariant 7)."""
    from filearr.worker import reap_stalled_jobs_now

    lib = "44444444-4444-4444-4444-444444444444"
    with _conn(proc_app_storm) as conn:
        # worker died AFTER the ScanRun committed: ScanRun stuck 'running'.
        conn.execute(
            "INSERT INTO scan_runs (library_id, status, rel_path) "
            "VALUES (%s::uuid, 'running', NULL)",
            (lib,),
        )
        jid = _insert_scan_job(conn, library_id=lib, status="doing", worker_id=None)

    counts = await reap_stalled_jobs_now()
    assert counts["failed"] >= 1

    with _conn(proc_app_storm) as conn:
        assert _job_status(conn, jid) == "failed"
        row = conn.execute(
            "SELECT status, stats FROM scan_runs WHERE library_id=%s::uuid", (lib,)
        ).fetchone()
    assert row[0] == "failed"
    assert row[1].get("reaped") is True


@pytestmark_async
async def test_reaper_scanrun_fail_is_scope_targeted(proc_app_storm):
    """Reaping a FULL scan orphan fails only the full ScanRun (rel_path NULL); a
    concurrent scoped ScanRun for the same library is untouched."""
    from filearr.worker import reap_stalled_jobs_now

    lib = "55555555-5555-5555-5555-555555555555"
    with _conn(proc_app_storm) as conn:
        conn.execute(
            "INSERT INTO scan_runs (library_id, status, rel_path) VALUES "
            "(%s::uuid, 'running', NULL), (%s::uuid, 'running', 'Sub')",
            (lib, lib),
        )
        _insert_scan_job(conn, library_id=lib, status="doing", worker_id=None)

    await reap_stalled_jobs_now()

    with _conn(proc_app_storm) as conn:
        full = conn.execute(
            "SELECT status FROM scan_runs WHERE library_id=%s::uuid AND rel_path IS NULL",
            (lib,),
        ).fetchone()[0]
        scoped = conn.execute(
            "SELECT status FROM scan_runs WHERE library_id=%s::uuid AND rel_path='Sub'",
            (lib,),
        ).fetchone()[0]
    assert full == "failed"      # the reaped full-scan orphan's run
    assert scoped == "running"   # scoped run left alone (different scope)


@pytestmark_async
async def test_reaper_caps_requeue_for_stuck_nonscan_job(proc_app_storm):
    """A NON-scan orphan that has already burned reap_max_attempts is FAILED, not
    requeued forever (the live attempts=50/51 runaway)."""
    from filearr.config import get_settings
    from filearr.worker import reap_stalled_jobs_now

    cap = get_settings().reap_max_attempts
    with _conn(proc_app_storm) as conn:
        jid = conn.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, args, status, worker_id, attempts) "
            "VALUES ('extract', %s, '{}'::jsonb, 'doing', NULL, %s) RETURNING id",
            (EXTRACT_TASK, cap),
        ).fetchone()[0]

    counts = await reap_stalled_jobs_now()
    assert counts["failed"] >= 1
    assert counts["retried"] == 0
    with _conn(proc_app_storm) as conn:
        assert _job_status(conn, jid) == "failed"


@pytestmark_async
async def test_reaper_requeues_stuck_nonscan_under_cap(proc_app_storm):
    """Below the cap, a NON-scan orphan is still requeued (doing->todo)."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_storm) as conn:
        jid = conn.execute(
            "INSERT INTO procrastinate_jobs "
            "(queue_name, task_name, args, status, worker_id, attempts) "
            "VALUES ('extract', %s, '{}'::jsonb, 'doing', NULL, 0) RETURNING id",
            (EXTRACT_TASK,),
        ).fetchone()[0]

    counts = await reap_stalled_jobs_now()
    assert counts["retried"] == 1
    with _conn(proc_app_storm) as conn:
        assert _job_status(conn, jid) == "todo"
