"""FIX-8 — failed-jobs history curation: retention purge, pagination totals,
and the manual clear-failed endpoint.

The failed-jobs list on the Admin + Jobs pages grew UNBOUNDED (procrastinate
never purges terminal rows on its own). This covers:
  * ``worker.purge_job_history_now`` — deletes terminal rows (succeeded / failed
    / cancelled / aborted) whose latest event is older than the retention window,
    while NEVER touching todo/doing or recent rows;
  * ``errors.failed_jobs_count`` + ``errors.failed_jobs`` pagination totals;
  * the ``/system/failed-jobs`` page shape ``{items, total, limit, offset}``;
  * ``POST /system/jobs/clear-failed`` — count, queue filter, admin gate.

Real Procrastinate schema on a throwaway pgserver Postgres (mirrors
``test_jobs_dashboard_uit10.py`` / ``test_stalled_reaper_fix6.py``): direct
INSERTs, read-back. delete_old_jobs measures a job's final-state age from its
LATEST ``procrastinate_events`` row (there is no finished_at column), so each
seeded terminal job gets one event at a chosen age.
"""

from __future__ import annotations

from pathlib import Path

import pytest

procrastinate = pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent

RETENTION_HOURS = 14 * 24  # matches the job_history_retention_days default


@pytest.fixture(scope="module")
def proc_pg(module_db):
    return module_db


@pytest.fixture
async def proc_app_pg(proc_pg):
    """Bind proc_app to the throwaway PG (procrastinate schema + app tables in the
    same DB), truncate the queue tables between tests, and yield inside an OPEN
    app so purge_job_history_now's job_manager/connector calls hit this DB."""
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
                conn.execute(
                    "TRUNCATE procrastinate_jobs, procrastinate_events, "
                    "procrastinate_workers RESTART IDENTITY CASCADE"
                )
            yield proc_pg
    proc_app.connector = original


def _conn(proc_pg):
    import psycopg

    return psycopg.connect(proc_pg.get_uri(), autocommit=True)


def _session_maker(proc_pg):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    uri = proc_pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _insert_job(conn, *, status, queue="extract", task="filearr.tasks.extract.extract_item"):
    return conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
        "VALUES (%s, %s, '{}'::jsonb, %s::procrastinate_job_status) RETURNING id",
        (queue, task, status),
    ).fetchone()[0]


def _insert_event(conn, job_id, *, hours_ago):
    # delete_old_jobs uses max(event.at) regardless of type, so any type works.
    conn.execute(
        "INSERT INTO procrastinate_events (job_id, type, at) "
        "VALUES (%s, 'started'::procrastinate_job_event_type, "
        "        NOW() - make_interval(hours => %s))",
        (job_id, hours_ago),
    )


def _ids(conn):
    return {
        r[0]
        for r in conn.execute("SELECT id FROM procrastinate_jobs").fetchall()
    }


# --------------------------------------------------------------------------- #
# retention purge                                                             #
# --------------------------------------------------------------------------- #
async def test_purge_removes_old_terminal_keeps_recent_and_active(proc_app_pg):
    """Old succeeded/failed/cancelled/aborted rows are deleted; recent terminal
    rows and todo/doing rows survive regardless of age."""
    from filearr.worker import purge_job_history_now

    old = RETENTION_HOURS + 100
    with _conn(proc_app_pg) as conn:
        succ_old = _insert_job(conn, status="succeeded")
        fail_old = _insert_job(conn, status="failed")
        canc_old = _insert_job(conn, status="cancelled")
        abrt_old = _insert_job(conn, status="aborted")
        for jid in (succ_old, fail_old, canc_old, abrt_old):
            _insert_event(conn, jid, hours_ago=old)

        fail_recent = _insert_job(conn, status="failed")
        succ_recent = _insert_job(conn, status="succeeded")
        _insert_event(conn, fail_recent, hours_ago=1)
        _insert_event(conn, succ_recent, hours_ago=1)

        todo = _insert_job(conn, status="todo")
        doing = _insert_job(conn, status="doing")
        _insert_event(conn, doing, hours_ago=old)  # old but not a final state

    result = await purge_job_history_now()

    assert result["deleted"] == 4
    assert result["by_status"] == {
        "succeeded": 1, "failed": 1, "cancelled": 1, "aborted": 1
    }
    with _conn(proc_app_pg) as conn:
        remaining = _ids(conn)
    assert remaining == {fail_recent, succ_recent, todo, doing}


async def test_purge_noop_when_nothing_old(proc_app_pg):
    """A queue of only-recent terminal rows is left untouched (deleted=0)."""
    from filearr.worker import purge_job_history_now

    with _conn(proc_app_pg) as conn:
        for _ in range(3):
            jid = _insert_job(conn, status="failed")
            _insert_event(conn, jid, hours_ago=1)

    result = await purge_job_history_now()
    assert result["deleted"] == 0
    with _conn(proc_app_pg) as conn:
        assert len(_ids(conn)) == 3


async def test_purge_bare_schema_returns_zero(pg_uri):
    """On a DB without the procrastinate schema, purge is total (all-zeros)."""
    import psycopg
    from procrastinate import PsycopgConnector

    from filearr.worker import proc_app, purge_job_history_now

    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bare_fix8")
        c.execute("CREATE DATABASE bare_fix8")
    # pgserver URIs carry the socket host in the query string (?host=/path), so
    # only the dbname path segment may be swapped (mirrors test_jobs_dashboard).
    if "/postgres?" in pg_uri:
        bare_dsn = pg_uri.replace("/postgres?", "/bare_fix8?", 1)
    else:
        head, query = pg_uri.rsplit("?", 1)
        bare_dsn = head.rsplit("/", 1)[0] + "/bare_fix8?" + query

    connector = PsycopgConnector(conninfo=bare_dsn)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            result = await purge_job_history_now()
    proc_app.connector = original
    assert result == {"deleted": 0, "by_status": {}}


# --------------------------------------------------------------------------- #
# pagination totals                                                           #
# --------------------------------------------------------------------------- #
async def test_failed_jobs_count_and_pagination(proc_app_pg):
    """failed_jobs_count returns the full failed total; failed_jobs pages it."""
    from filearr.errors import failed_jobs, failed_jobs_count

    with _conn(proc_app_pg) as conn:
        for _ in range(7):
            _insert_job(conn, status="failed")
        _insert_job(conn, status="succeeded")  # not counted
        _insert_job(conn, status="todo")  # not counted

    engine, Session = _session_maker(proc_app_pg)
    async with Session() as session:
        total = await failed_jobs_count(session)
        page1 = await failed_jobs(session, limit=5, offset=0)
        page2 = await failed_jobs(session, limit=5, offset=5)
    await engine.dispose()

    assert total == 7
    assert len(page1) == 5
    assert len(page2) == 2
    assert all(j["status"] == "failed" for j in page1 + page2)


async def test_failed_jobs_view_page_shape(proc_app_pg):
    """The endpoint returns {items, total, limit, offset} (FIX-8)."""
    from filearr.api.system import failed_jobs_view

    with _conn(proc_app_pg) as conn:
        for _ in range(3):
            _insert_job(conn, status="failed")

    engine, Session = _session_maker(proc_app_pg)
    async with Session() as session:
        out = await failed_jobs_view(limit=2, offset=0, session=session)
    await engine.dispose()

    assert out["total"] == 3
    assert out["limit"] == 2
    assert out["offset"] == 0
    assert len(out["items"]) == 2


# --------------------------------------------------------------------------- #
# manual clear-failed                                                         #
# --------------------------------------------------------------------------- #
async def test_clear_failed_deletes_all_failed_only(proc_app_pg):
    """clear-failed removes every failed row and returns the count; todo/doing/
    succeeded are untouched."""
    from filearr.api.system import ClearFailedJobs, clear_failed_jobs

    with _conn(proc_app_pg) as conn:
        for _ in range(4):
            _insert_job(conn, status="failed")
        keep_todo = _insert_job(conn, status="todo")
        keep_succ = _insert_job(conn, status="succeeded")

    engine, Session = _session_maker(proc_app_pg)
    async with Session() as session:
        out = await clear_failed_jobs(ClearFailedJobs(), session)
    await engine.dispose()

    assert out["deleted"] == 4
    assert out["queue"] is None
    with _conn(proc_app_pg) as conn:
        assert _ids(conn) == {keep_todo, keep_succ}


async def test_clear_failed_queue_filter(proc_app_pg):
    """clear-failed with a queue only removes that queue's failed rows."""
    from filearr.api.system import ClearFailedJobs, clear_failed_jobs

    with _conn(proc_app_pg) as conn:
        for _ in range(3):
            _insert_job(conn, status="failed", queue="extract")
        keep = [_insert_job(conn, status="failed", queue="index") for _ in range(2)]

    engine, Session = _session_maker(proc_app_pg)
    async with Session() as session:
        out = await clear_failed_jobs(ClearFailedJobs(queue="extract"), session)
    await engine.dispose()

    assert out["deleted"] == 3
    assert out["queue"] == "extract"
    with _conn(proc_app_pg) as conn:
        assert _ids(conn) == set(keep)


async def test_clear_failed_endpoint_requires_admin(pg_uri, monkeypatch):
    """POST /system/jobs/clear-failed rejects a missing token (401) and a
    read-only key (403) BEFORE the delete body runs (admin scope enforced)."""
    import httpx
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app
    from filearr.models import ApiKey
    from filearr.security import generate_key

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM api_keys"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", True)

    full, prefix, key_hash = generate_key()
    async with maker() as s:
        s.add(ApiKey(name="read-only", prefix=prefix, key_hash=key_hash, scopes=["read"]))
        await s.commit()

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/v1/system/jobs/clear-failed")
            assert r.status_code == 401
            r = await c.post(
                "/api/v1/system/jobs/clear-failed",
                headers={"Authorization": f"Bearer {full}"},
                json={},
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        get_settings.cache_clear()
