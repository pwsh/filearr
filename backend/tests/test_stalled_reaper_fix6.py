"""FIX-6 — stalled-job reaper + richer job status.

The reaper requeues or fails jobs orphaned in ``doing`` by a dead/restarted
worker. procrastinate SETs ``job.worker_id`` NULL when it prunes a dead worker
row (FK ``ON DELETE SET NULL``), so an orphan surfaces in the heartbeat net.

Detection has two independent nets (see ``worker._DETECT_STALLED_SQL``):
  * heartbeat net (ALL doing jobs): worker_id NULL or worker heartbeat stale,
  * age net (NON-scan doing jobs only): running longer than the age ceiling;
    ``scan_library`` is EXEMPT (a full walk legitimately runs long).

Load-bearing lock finding: the ``queueing_lock`` unique index is partial
(``WHERE status = 'todo'`` only). A ``doing`` job holds NO queueing lock;
retrying it to ``todo`` RE-establishes the lock. If a replacement ``todo`` job
already holds it, the retry collides (``UniqueViolation``) and the reaper FAILS
the orphan instead — so ticks are idempotent and never duplicate locked work.

Real Procrastinate schema on a throwaway pgserver Postgres (mirrors
``test_jobs_dashboard_uit10.py``): direct INSERTs, read-back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

procrastinate = pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent

SCAN_TASK = "filearr.tasks.scan.scan_library"
EXTRACT_TASK = "filearr.tasks.extract.extract_item"


@pytest.fixture(scope="module")
def proc_pg(module_db):
    return module_db


@pytest.fixture
async def proc_app_pg(proc_pg):
    """Bind proc_app to the throwaway PG, apply the procrastinate schema, and
    truncate the queue tables between tests. Yields inside an OPEN app so the
    reaper's job_manager/connector calls hit this DB."""
    import psycopg
    from procrastinate import PsycopgConnector

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


def _insert_worker(conn, *, seconds_ago: int) -> int:
    return conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (NOW() - make_interval(secs => %s)) RETURNING id",
        (seconds_ago,),
    ).fetchone()[0]


def _insert_job(
    conn, *, task=EXTRACT_TASK, queue="extract", status="doing",
    worker_id=None, queueing_lock=None, args="{}",
) -> int:
    return conn.execute(
        "INSERT INTO procrastinate_jobs "
        "(queue_name, task_name, args, status, worker_id, queueing_lock) "
        "VALUES (%s, %s, %s::jsonb, %s::procrastinate_job_status, %s, %s) RETURNING id",
        (queue, task, args, status, worker_id, queueing_lock),
    ).fetchone()[0]


def _insert_started(conn, job_id: int, *, seconds_ago: int) -> None:
    conn.execute(
        "INSERT INTO procrastinate_events (job_id, type, at) "
        "VALUES (%s, 'started'::procrastinate_job_event_type, "
        "        NOW() - make_interval(secs => %s))",
        (job_id, seconds_ago),
    )


def _status(conn, job_id: int) -> str:
    return conn.execute(
        "SELECT status::text FROM procrastinate_jobs WHERE id = %s", (job_id,)
    ).fetchone()[0]


def _lock(conn, job_id: int) -> str | None:
    return conn.execute(
        "SELECT queueing_lock FROM procrastinate_jobs WHERE id = %s", (job_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------------- #
# heartbeat net                                                               #
# --------------------------------------------------------------------------- #
async def test_stale_worker_is_reaped_and_pruned(proc_app_pg):
    """A doing job whose worker's heartbeat is stale is requeued; the dead
    worker row is pruned (covers prune_stalled_workers)."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        wid = _insert_worker(conn, seconds_ago=300)  # stale (>30s)
        jid = _insert_job(conn, worker_id=wid)

    counts = await reap_stalled_jobs_now()

    assert counts["pruned_workers"] >= 1
    assert counts["retried"] == 1
    assert counts["reaped"] == 1
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "todo"  # requeued to run again


async def test_alive_worker_job_not_reaped(proc_app_pg):
    """A doing job with a freshly-heartbeating worker is left alone."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        wid = _insert_worker(conn, seconds_ago=2)  # fresh (<30s)
        jid = _insert_job(conn, worker_id=wid)
        _insert_started(conn, jid, seconds_ago=10)  # young, no age-net trip

    counts = await reap_stalled_jobs_now()

    assert counts["reaped"] == 0
    assert counts["pruned_workers"] == 0
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "doing"


async def test_null_worker_job_is_reaped(proc_app_pg):
    """A doing job with NULL worker_id (worker already pruned) is requeued."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        jid = _insert_job(conn, worker_id=None)

    counts = await reap_stalled_jobs_now()

    assert counts["retried"] == 1
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "todo"


# --------------------------------------------------------------------------- #
# age net + scan exemption                                                    #
# --------------------------------------------------------------------------- #
async def test_age_net_reaps_long_running_non_scan(proc_app_pg):
    """A NON-scan doing job with a LIVE worker but running past the age ceiling
    is reaped via the absolute-age net."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        wid = _insert_worker(conn, seconds_ago=2)  # alive
        jid = _insert_job(conn, task=EXTRACT_TASK, worker_id=wid)
        _insert_started(conn, jid, seconds_ago=7200)  # 2h > 3600s ceiling

    counts = await reap_stalled_jobs_now()

    assert counts["retried"] == 1
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "todo"


async def test_scan_library_exempt_from_age_net(proc_app_pg):
    """A scan_library job with a LIVE worker, running long, is NOT reaped: the
    age net exempts scans (a full library walk legitimately runs long)."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        wid = _insert_worker(conn, seconds_ago=2)  # alive
        jid = _insert_job(conn, task=SCAN_TASK, queue="scan", worker_id=wid)
        _insert_started(conn, jid, seconds_ago=7200)  # long, but scan is exempt

    counts = await reap_stalled_jobs_now()

    assert counts["reaped"] == 0
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "doing"


# --------------------------------------------------------------------------- #
# retry semantics + lock finding                                              #
# --------------------------------------------------------------------------- #
async def test_retry_reestablishes_queueing_lock(proc_app_pg):
    """Requeuing a doing job to todo preserves its queueing_lock, which the
    partial unique index now re-enforces (the load-bearing lock finding)."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        jid = _insert_job(conn, worker_id=None, queueing_lock="lock:a")

    await reap_stalled_jobs_now()

    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "todo"
        assert _lock(conn, jid) == "lock:a"


async def test_scan_orphan_is_failed_not_retried(proc_app_pg):
    """An orphaned scan_library job is FAILED (its ScanRun is already crash-
    failed; operators retrigger) rather than silently re-run."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        jid = _insert_job(conn, task=SCAN_TASK, queue="scan", worker_id=None)

    counts = await reap_stalled_jobs_now()

    assert counts["failed"] == 1
    assert counts["retried"] == 0
    with _conn(proc_app_pg) as conn:
        assert _status(conn, jid) == "failed"


async def test_collision_falls_back_to_failed(proc_app_pg):
    """If a replacement todo job already holds the orphan's queueing_lock, the
    retry collides and the orphan is FAILED instead — todo holder untouched."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        todo_id = _insert_job(conn, status="todo", queueing_lock="dup")
        orphan_id = _insert_job(conn, status="doing", worker_id=None, queueing_lock="dup")

    counts = await reap_stalled_jobs_now()

    assert counts["failed"] == 1
    assert counts["retried"] == 0
    with _conn(proc_app_pg) as conn:
        assert _status(conn, orphan_id) == "failed"  # orphan failed on collision
        assert _status(conn, todo_id) == "todo"  # replacement untouched


async def test_reaper_is_idempotent(proc_app_pg):
    """A second run over the just-reaped state acts on nothing new (requeued
    jobs are now todo, failed jobs are terminal — neither is doing)."""
    from filearr.worker import reap_stalled_jobs_now

    with _conn(proc_app_pg) as conn:
        wid = _insert_worker(conn, seconds_ago=300)
        _insert_job(conn, worker_id=wid)  # heartbeat-net orphan
        _insert_job(conn, task=SCAN_TASK, queue="scan", worker_id=None)  # scan orphan

    first = await reap_stalled_jobs_now()
    assert first["reaped"] == 2

    second = await reap_stalled_jobs_now()
    assert second == {"reaped": 0, "retried": 0, "failed": 0, "pruned_workers": 0,
                      "scan_runs_reconciled": 0}  # FIX-15 key present, no-op here


# --------------------------------------------------------------------------- #
# endpoint admin gate                                                         #
# --------------------------------------------------------------------------- #
async def test_reap_endpoint_requires_admin(pg_uri, monkeypatch):
    """POST /system/jobs/reap rejects a missing token (401) and a read-only key
    (403) BEFORE the reaper body runs (admin scope enforced)."""
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
            # no token -> 401
            r = await c.post("/api/v1/system/jobs/reap")
            assert r.status_code == 401
            # read-only key lacks admin -> 403
            r = await c.post(
                "/api/v1/system/jobs/reap",
                headers={"Authorization": f"Bearer {full}"},
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        get_settings.cache_clear()
