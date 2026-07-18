"""FIX-15 — stuck-``stopping`` ScanRun reconciliation + manual force-clear.

Root cause: the graceful-stop transition (``stopping`` -> ``stopped``) only ever
runs inside a LIVE scan worker's between-batch check, and the stalled-job reaper
only transitions a running/stopping ScanRun when it detects a *stalled ``doing``
scan job* that same tick. A ``stopping`` (or orphaned ``running``) ScanRun whose
job has LEFT ``doing`` (succeeded / failed / cancelled / aborted / purged from
job history) has no stalled job to reap and never converges -- and the
scheduler's running-row guard (``status IN ('running','stopping')``) then blocks
every scheduled scan for that library forever.

Two layers, mirroring the reaper/storm tests' real-Procrastinate-on-pgserver
style:
  * reconciler + ``scan_job_active`` unit matrix over a throwaway PG that carries
    BOTH the procrastinate schema and a minimal ``scan_runs`` table;
  * force-clear + hardened-stop API matrix (admin gate, audit) over the app DB.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCAN_TASK = "filearr.tasks.scan.scan_library"

procrastinate = pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# throwaway PG: procrastinate schema + a minimal scan_runs table              #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def recon_pg(module_db):
    # module_db already carries the uuidv7() shim; add the minimal scan_runs
    # table this test's reaper UPDATE touches.
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
async def proc_app_recon(recon_pg):
    import psycopg
    from procrastinate import PsycopgConnector

    from filearr.worker import proc_app

    dsn = recon_pg.get_uri()
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
            yield recon_pg
    proc_app.connector = original


def _conn(pg):
    import psycopg

    return psycopg.connect(pg.get_uri(), autocommit=True)


def _insert_run(conn, *, library_id, status, rel_path=None, age_seconds=1800) -> str:
    return conn.execute(
        "INSERT INTO scan_runs (library_id, status, rel_path, started_at) "
        "VALUES (%s::uuid, %s, %s, NOW() - make_interval(secs => %s)) RETURNING id",
        (library_id, status, rel_path, age_seconds),
    ).fetchone()[0]


def _insert_scan_job(conn, *, library_id, rel_path=None, status="doing",
                     worker_id=None) -> int:
    import json

    args = {"library_id": library_id}
    if rel_path is not None:
        args["rel_path"] = rel_path
    return conn.execute(
        "INSERT INTO procrastinate_jobs "
        "(queue_name, task_name, args, status, worker_id) "
        "VALUES ('scan', %s, %s::jsonb, %s::procrastinate_job_status, %s) RETURNING id",
        (SCAN_TASK, json.dumps(args), status, worker_id),
    ).fetchone()[0]


def _insert_worker(conn, *, seconds_ago) -> int:
    return conn.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (NOW() - make_interval(secs => %s)) RETURNING id",
        (seconds_ago,),
    ).fetchone()[0]


def _run(conn, rid):
    return conn.execute(
        "SELECT status, stats, finished_at FROM scan_runs WHERE id=%s", (rid,)
    ).fetchone()


def _lib() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# reconciler matrix                                                           #
# --------------------------------------------------------------------------- #
async def test_stopping_no_job_becomes_stopped(proc_app_recon):
    from filearr.worker import reconcile_orphaned_scan_runs_now

    lib = _lib()
    with _conn(proc_app_recon) as conn:
        rid = _insert_run(conn, library_id=lib, status="stopping")

    out = await reconcile_orphaned_scan_runs_now()
    assert out == {"reconciled": 1, "stopped": 1, "failed": 0}

    with _conn(proc_app_recon) as conn:
        status, stats, finished = _run(conn, rid)
    assert status == "stopped"          # honor the operator's stop intent
    assert finished is not None
    assert stats.get("reconciled") is True
    assert "reconcile_note" in stats


async def test_running_no_job_becomes_failed(proc_app_recon):
    from filearr.worker import reconcile_orphaned_scan_runs_now

    lib = _lib()
    with _conn(proc_app_recon) as conn:
        rid = _insert_run(conn, library_id=lib, status="running")

    out = await reconcile_orphaned_scan_runs_now()
    assert out == {"reconciled": 1, "stopped": 0, "failed": 1}

    with _conn(proc_app_recon) as conn:
        status, stats, finished = _run(conn, rid)
    assert status == "failed"           # invariant 7 preserved for orphaned running
    assert finished is not None
    assert stats.get("reconciled") is True


async def test_live_job_untouched(proc_app_recon):
    """A stopping/running run WITH an unfinished scan job (todo/doing/aborting)
    for its (library, scope) is left alone -- a live worker will finish it."""
    from filearr.worker import reconcile_orphaned_scan_runs_now

    rids = []
    with _conn(proc_app_recon) as conn:
        for st in ("todo", "doing", "aborting"):
            lib = _lib()
            rids.append(_insert_run(conn, library_id=lib, status="stopping"))
            _insert_scan_job(conn, library_id=lib, status=st)

    out = await reconcile_orphaned_scan_runs_now()
    assert out["reconciled"] == 0

    with _conn(proc_app_recon) as conn:
        for rid in rids:
            assert _run(conn, rid)[0] == "stopping"


async def test_grace_period_respected(proc_app_recon):
    """A young non-terminal run (started_at within the grace window) is NOT swept
    even with no job -- covers the just-enqueued-but-job-not-yet-visible race."""
    from filearr.worker import reconcile_orphaned_scan_runs_now

    lib = _lib()
    with _conn(proc_app_recon) as conn:
        rid = _insert_run(conn, library_id=lib, status="stopping", age_seconds=5)

    out = await reconcile_orphaned_scan_runs_now()
    assert out["reconciled"] == 0
    with _conn(proc_app_recon) as conn:
        assert _run(conn, rid)[0] == "stopping"


async def test_reconcile_is_idempotent(proc_app_recon):
    from filearr.worker import reconcile_orphaned_scan_runs_now

    with _conn(proc_app_recon) as conn:
        _insert_run(conn, library_id=_lib(), status="stopping")
        _insert_run(conn, library_id=_lib(), status="running")

    first = await reconcile_orphaned_scan_runs_now()
    assert first["reconciled"] == 2
    second = await reconcile_orphaned_scan_runs_now()
    assert second == {"reconciled": 0, "stopped": 0, "failed": 0}


async def test_scope_targeting(proc_app_recon):
    """A scoped job protects only its OWN scope; a full-scan stopping run is NOT
    protected by a scoped job (and vice-versa)."""
    from filearr.worker import reconcile_orphaned_scan_runs_now

    lib = _lib()
    with _conn(proc_app_recon) as conn:
        full = _insert_run(conn, library_id=lib, status="stopping", rel_path=None)
        scoped = _insert_run(conn, library_id=lib, status="stopping", rel_path="Sub")
        # Only a scoped 'Sub' job exists: it protects the scoped run, not the full.
        _insert_scan_job(conn, library_id=lib, rel_path="Sub", status="doing")

    out = await reconcile_orphaned_scan_runs_now()
    assert out["reconciled"] == 1
    with _conn(proc_app_recon) as conn:
        assert _run(conn, full)[0] == "stopped"     # full run reconciled
        assert _run(conn, scoped)[0] == "stopping"  # scoped run protected


async def test_reaper_now_includes_reconcile(proc_app_recon):
    """reap_stalled_jobs_now sweeps a stuck stopping run whose job is GONE and
    reports it under scan_runs_reconciled (the maintenance tick path)."""
    from filearr.worker import reap_stalled_jobs_now

    lib = _lib()
    with _conn(proc_app_recon) as conn:
        rid = _insert_run(conn, library_id=lib, status="stopping")  # no job at all

    counts = await reap_stalled_jobs_now()
    assert counts["scan_runs_reconciled"] >= 1
    with _conn(proc_app_recon) as conn:
        assert _run(conn, rid)[0] == "stopped"


# --------------------------------------------------------------------------- #
# scan_job_active tri-state                                                    #
# --------------------------------------------------------------------------- #
async def test_scan_job_active_live_vs_stale(proc_app_recon):
    from filearr.worker import scan_job_active

    lib_live = _lib()
    lib_stale = _lib()
    lib_none = _lib()
    with _conn(proc_app_recon) as conn:
        fresh = _insert_worker(conn, seconds_ago=2)
        stale = _insert_worker(conn, seconds_ago=600)
        _insert_scan_job(conn, library_id=lib_live, status="doing", worker_id=fresh)
        _insert_scan_job(conn, library_id=lib_stale, status="doing", worker_id=stale)

    assert await scan_job_active(lib_live, None) is True     # fresh worker draining
    assert await scan_job_active(lib_stale, None) is False   # stale worker = orphan
    assert await scan_job_active(lib_none, None) is False     # no job at all


# --------------------------------------------------------------------------- #
# force-clear + hardened-stop API matrix (app DB via alembic)                  #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def api(pg_uri, monkeypatch):
    """FastAPI app on the app DB (alembic head), auth OFF, audit + get_session
    bound to the same DB. No procrastinate schema here -> scan_job_active is
    unknown (None) / proc_app.open_async fails fast on the default DSN, so
    force-clear proceeds and stop keeps its graceful path unless overridden."""
    import httpx
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)  # audit writes here too
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()


async def _mk_run(maker, status):
    from filearr.models import Library, ScanRun

    async with maker() as s:
        lib = Library(name=f"l-{uuid.uuid4()}", root_path="/data")
        s.add(lib)
        await s.flush()
        run = ScanRun(library_id=lib.id, status=status, stats={},
                      started_at=datetime.now(UTC))
        s.add(run)
        await s.commit()
        return str(run.id)


async def test_force_clear_nonterminal_to_stopped(api):
    from sqlalchemy import select, text

    c, maker = api
    rid = await _mk_run(maker, "stopping")
    r = await c.post(f"/api/v1/scans/{rid}/force-clear")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stopped" and body["previous_status"] == "stopping"

    from filearr.models import ScanRun

    async with maker() as s:
        row = (await s.execute(select(ScanRun).where(ScanRun.id == uuid.UUID(rid)))).scalar_one()
        assert row.status == "stopped"
        assert row.finished_at is not None
        assert row.stats.get("force_cleared") is True
        # audited in security_events
        n = (await s.execute(text(
            "SELECT count(*) FROM security_events WHERE event_type='scan_force_cleared'"
        ))).scalar_one()
        assert n == 1


async def test_force_clear_running_also_clears(api):
    c, maker = api
    rid = await _mk_run(maker, "running")
    r = await c.post(f"/api/v1/scans/{rid}/force-clear")
    assert r.status_code == 200 and r.json()["status"] == "stopped"


async def test_force_clear_terminal_409(api):
    c, maker = api
    for st in ("finished", "failed", "cancelled", "stopped"):
        rid = await _mk_run(maker, st)
        r = await c.post(f"/api/v1/scans/{rid}/force-clear")
        assert r.status_code == 409, st


async def test_force_clear_unknown_404(api):
    c, _ = api
    r = await c.post(f"/api/v1/scans/{uuid.uuid4()}/force-clear")
    assert r.status_code == 404


async def test_force_clear_refuses_active(api, monkeypatch):
    """A genuinely-active run (live worker) is refused with 409 'use stop'."""
    import filearr.worker as worker

    c, maker = api
    rid = await _mk_run(maker, "running")

    @contextlib.asynccontextmanager
    async def _fake_open():
        yield

    async def _active(*a, **k):
        return True

    monkeypatch.setattr(worker.proc_app, "open_async", _fake_open)
    monkeypatch.setattr(worker, "scan_job_active", _active)

    r = await c.post(f"/api/v1/scans/{rid}/force-clear")
    assert r.status_code == 409
    assert "still active" in r.json()["detail"]


async def test_stop_orphaned_run_finalizes_directly(api, monkeypatch):
    """FIX-15 stop hardening: when no live worker is draining the run, /stop
    finalizes to terminal 'stopped' in one step (instead of a 'stopping' marker
    that would never converge)."""
    import filearr.worker as worker

    c, maker = api
    rid = await _mk_run(maker, "running")

    @contextlib.asynccontextmanager
    async def _fake_open():
        yield

    async def _inactive(*a, **k):
        return False  # positively no live worker

    monkeypatch.setattr(worker.proc_app, "open_async", _fake_open)
    monkeypatch.setattr(worker, "scan_job_active", _inactive)

    r = await c.post(f"/api/v1/scans/{rid}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"

    from sqlalchemy import select

    from filearr.models import ScanRun

    async with maker() as s:
        row = (await s.execute(select(ScanRun).where(ScanRun.id == uuid.UUID(rid)))).scalar_one()
    assert row.status == "stopped" and row.finished_at is not None


async def test_stop_keeps_graceful_when_active(api, monkeypatch):
    """When a live worker IS draining the run, /stop keeps the legacy graceful
    'stopping' marker for the worker to observe."""
    import filearr.worker as worker

    c, maker = api
    rid = await _mk_run(maker, "running")

    @contextlib.asynccontextmanager
    async def _fake_open():
        yield

    async def _active(*a, **k):
        return True

    monkeypatch.setattr(worker.proc_app, "open_async", _fake_open)
    monkeypatch.setattr(worker, "scan_job_active", _active)

    r = await c.post(f"/api/v1/scans/{rid}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopping"


async def test_force_clear_requires_admin(pg_uri, monkeypatch):
    """force-clear is admin-gated: 401 without a token, 403 with a read-only key
    (auth ENABLED)."""
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
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM api_keys"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", True)

    full, prefix, key_hash = generate_key()
    async with maker() as s:
        s.add(ApiKey(name="ro", prefix=prefix, key_hash=key_hash, scopes=["read"]))
        await s.commit()

    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            sid = uuid.uuid4()
            r = await c.post(f"/api/v1/scans/{sid}/force-clear")
            assert r.status_code == 401
            r = await c.post(
                f"/api/v1/scans/{sid}/force-clear",
                headers={"Authorization": f"Bearer {full}"},
            )
            assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        get_settings.cache_clear()
