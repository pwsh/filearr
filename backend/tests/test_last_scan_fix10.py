"""FIX-10 — GET /libraries returns per-library ``last_scan`` from scan_runs.

Root cause of the live bug: the Admin page derived "Last scan" by filtering the
capped, global ``GET /scans`` feed (``limit 50``), so a library whose most-recent
run fell out of that window (e.g. after a redeploy triggered a burst of new runs)
rendered as "never scanned" even though its ScanRun history was intact in
Postgres. The fix sources ``last_scan`` per-library directly from ``scan_runs``
(DISTINCT ON), so it survives restarts and is not window-dependent.

These tests drive the real app over ASGI (mirrors test_libraries_api_t7 wiring)
and seed ScanRun rows directly to assert the endpoint's behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import ScanRun

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE = "/api/v1/libraries"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client_and_maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _make_library(client, name="L", root="/data/l"):
    r = await client.post(BASE, json={"name": name, "root_path": root})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_run(maker, library_id, *, status, started_at, stats=None, finished=True):
    async with maker() as s:
        run = ScanRun(
            library_id=library_id,
            status=status,
            started_at=started_at,
            finished_at=(started_at + timedelta(seconds=30)) if finished else None,
            stats=stats or {},
        )
        s.add(run)
        await s.commit()
        return run.id


async def _last_scan(client, library_id):
    r = await client.get(BASE)
    assert r.status_code == 200, r.text
    lib = next(x for x in r.json() if x["id"] == library_id)
    return lib["last_scan"]


async def test_null_when_no_runs(client_and_maker):
    client, _ = client_and_maker
    lib_id = await _make_library(client)
    assert await _last_scan(client, lib_id) is None


async def test_returns_completed_run(client_and_maker):
    client, maker = client_and_maker
    lib_id = await _make_library(client)
    await _add_run(
        maker, lib_id, status="finished",
        started_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        stats={"seen": 100, "new": 5, "changed": 2, "missing": 1},
    )
    ls = await _last_scan(client, lib_id)
    assert ls is not None
    assert ls["status"] == "finished"
    assert ls["seen"] == 100
    assert ls["new"] == 5
    assert ls["changed"] == 2
    assert ls["missing"] == 1
    assert ls["finished_at"] is not None


@pytest.mark.parametrize("status", ["failed", "stopped", "cancelled"])
async def test_surfaces_non_finished_terminal_status(client_and_maker, status):
    # A failed/stopped/cancelled last scan must show its status, NOT "never ran".
    client, maker = client_and_maker
    lib_id = await _make_library(client)
    await _add_run(
        maker, lib_id, status=status,
        started_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
        stats={"error": "boom"} if status == "failed" else {},
    )
    ls = await _last_scan(client, lib_id)
    assert ls is not None
    assert ls["status"] == status


async def test_latest_run_wins(client_and_maker):
    # Ordering: the newest started_at is the reported last_scan, regardless of
    # insertion order or the older row's status.
    client, maker = client_and_maker
    lib_id = await _make_library(client)
    await _add_run(
        maker, lib_id, status="failed",
        started_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC), stats={"seen": 1},
    )
    await _add_run(
        maker, lib_id, status="finished",
        started_at=datetime(2026, 7, 12, 0, 0, tzinfo=UTC), stats={"seen": 999},
    )
    await _add_run(
        maker, lib_id, status="cancelled",
        started_at=datetime(2026, 6, 15, 0, 0, tzinfo=UTC), stats={"seen": 7},
    )
    ls = await _last_scan(client, lib_id)
    assert ls["status"] == "finished"
    assert ls["seen"] == 999


async def test_per_library_isolation_no_crosstalk(client_and_maker):
    # DISTINCT ON must attribute each library's latest run to that library only.
    client, maker = client_and_maker
    a = await _make_library(client, name="A", root="/data/a")
    b = await _make_library(client, name="B", root="/data/b")
    await _add_run(
        maker, a, status="finished",
        started_at=datetime(2026, 7, 5, 0, 0, tzinfo=UTC), stats={"seen": 10},
    )
    # B has no runs -> null; A -> its run.
    ls_a = await _last_scan(client, a)
    ls_b = await _last_scan(client, b)
    assert ls_a["seen"] == 10
    assert ls_b is None


async def test_libraries_schema_unchanged_for_existing_consumers(client_and_maker):
    # No regression: the existing library fields are all still present.
    client, _ = client_and_maker
    lib_id = await _make_library(client)
    r = await client.get(BASE)
    lib = next(x for x in r.json() if x["id"] == lib_id)
    for key in ("id", "name", "root_path", "enabled", "hash_policy", "scan_cron"):
        assert key in lib
    assert "last_scan" in lib
