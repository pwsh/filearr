"""FIX-12 — the Jobs page "attempts" number is honest but was misread.

procrastinate 3.9 keeps a SINGLE per-job ``attempts`` counter and no separate
reschedule counter, so ``extract_item``'s staged-pipeline gate *reschedules*
(waits while a scan walks the same library — zero work done) inflate ``attempts``
right alongside genuine failure-retries. A single file could therefore show
attempts in the dozens/hundreds during a big scan without ever having FAILED
(the user report). Pre-FIX-8 reaper requeues inflated it the same way.

The fix is presentational + honest: every running/failed job row now also carries
``retry_cap`` — the task's genuine-failure retry budget — so the UI renders
``attempts/cap`` and shows any excess as reschedules/requeues rather than failed
tries. These tests pin:

  * ``TASK_RETRY_CAPS`` stays in lock-step with the authoritative source constants
    (a budget change in one place can't silently drift the display);
  * ``retry_cap_for`` maps known tasks and returns ``None`` for no-retry/periodic/
    unknown tasks;
  * both the running list and the failed list surface the correct per-row cap
    (checked separately — the two lists are built by different queries).
"""

from __future__ import annotations

import pytest

from filearr.errors import TASK_RETRY_CAPS, retry_cap_for


async def test_retry_cap_map_matches_source_constants():
    # extract genuine-failure budget.
    from filearr.tasks.extract import EXTRACT_MAX_ATTEMPTS

    assert TASK_RETRY_CAPS["filearr.tasks.extract.extract_item"] == EXTRACT_MAX_ATTEMPTS

    # Meili-touching tasks share one exponential-retry budget.
    from filearr.retrying import MEILI_RETRY_MAX_ATTEMPTS

    assert (
        TASK_RETRY_CAPS["filearr.tasks.index_sync.sync_items"]
        == MEILI_RETRY_MAX_ATTEMPTS
    )
    assert (
        TASK_RETRY_CAPS["filearr.tasks.index_sync.rebuild_index"]
        == MEILI_RETRY_MAX_ATTEMPTS
    )

    # embed tasks are decorated with a literal ``retry=2`` (no named constant).
    assert TASK_RETRY_CAPS["filearr.tasks.embed.embed_item"] == 2
    assert TASK_RETRY_CAPS["filearr.tasks.embed.embed_missing"] == 2


async def test_retry_cap_for_lookups():
    assert retry_cap_for("filearr.tasks.extract.extract_item") == 2
    assert retry_cap_for("filearr.tasks.index_sync.sync_items") == 4
    # A periodic maintenance tick / scan carries NO retry -> None (a single
    # failure is terminal), so the UI shows just the raw attempts.
    assert retry_cap_for("filearr.tasks.scan.scan_library") is None
    assert retry_cap_for("filearr.worker.reap_stalled_jobs") is None
    assert retry_cap_for("something.unknown") is None
    assert retry_cap_for(None) is None
    assert retry_cap_for("") is None


# --- API-contract: per-row retry_cap on both the running AND failed lists ----
# Mirrors test_jobs_dashboard_uit10's throwaway-pgserver harness (real
# procrastinate schema, direct INSERTs, read-back). Skipped when pgserver is
# unavailable, like the sibling job-introspection tests.
pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def proc_pg(module_db):
    return module_db


@pytest.fixture
async def proc_connector(proc_pg):
    import psycopg
    from procrastinate import PsycopgConnector
    from sqlalchemy.ext.asyncio import create_async_engine

    from filearr.models import Base
    from filearr.worker import proc_app

    dsn = proc_pg.get_uri()
    connector = PsycopgConnector(conninfo=dsn)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            engine = create_async_engine(
                dsn.replace("postgresql://", "postgresql+psycopg://", 1)
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
                conn.execute("TRUNCATE items, scan_runs, libraries CASCADE")
            yield proc_pg
    proc_app.connector = original


def _session_maker(proc_pg):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    uri = proc_pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _insert_job(conn, queue, task, status, attempts=0):
    conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, attempts) "
        "VALUES (%s, %s, '{}'::jsonb, %s::procrastinate_job_status, %s) RETURNING id",
        (queue, task, status, attempts),
    )


async def test_running_list_surfaces_per_task_retry_cap(proc_connector):
    """A running extract job carries its EXTRACT_MAX_ATTEMPTS budget; a running
    scan (no retry) carries ``retry_cap == None`` even with inflated attempts."""
    import psycopg

    from filearr.jobs_stats import running_jobs

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        # Extract job whose attempts are hugely inflated by staged-gate reschedules
        # — the exact scenario the user reported ("attempted hundreds of times").
        _insert_job(
            conn, "extract", "filearr.tasks.extract.extract_item", "doing",
            attempts=137,
        )
        _insert_job(conn, "scan", "filearr.tasks.scan.scan_library", "doing", attempts=3)

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        jobs = await running_jobs(session)
    await engine.dispose()

    by_task = {j["task"]: j for j in jobs}
    # The raw attempts is preserved (honest) but paired with the budget so the UI
    # can render "2/2 (+135 waiting)" instead of a bare, alarming "137".
    assert by_task["extract_item"]["attempts"] == 137
    assert by_task["extract_item"]["retry_cap"] == 2
    # scan_library has no retry policy -> no cap.
    assert by_task["scan_library"]["retry_cap"] is None


async def test_failed_list_surfaces_per_task_retry_cap(proc_connector):
    """The failed list is built by a DIFFERENT query than the running list, so it
    is asserted separately (per the FIX-12 brief)."""
    import psycopg

    from filearr.errors import failed_jobs

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        _insert_job(
            conn, "index", "filearr.tasks.index_sync.sync_items", "failed", attempts=4
        )
        _insert_job(
            conn, "maintenance", "filearr.worker.purge_job_history", "failed", attempts=0
        )

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        rows = await failed_jobs(session)
    await engine.dispose()

    by_task = {r["task"]: r for r in rows}
    assert by_task["filearr.tasks.index_sync.sync_items"]["retry_cap"] == 4
    # A periodic maintenance task carries no retry -> null cap.
    assert by_task["filearr.worker.purge_job_history"]["retry_cap"] is None
