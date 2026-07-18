"""FIX-1 — retry-extracts must not re-queue items that already have a pending
extract job.

The never-hashed (quick_hash IS NULL) arm of POST /libraries/{id}/retry-extracts
previously matched the entire not-yet-extracted backlog, so retrying mid-scan
re-deferred hundreds of thousands of duplicate jobs. The fix anti-joins
``procrastinate_jobs`` (todo/doing on args->>'item_id'); the ``_extract_error``
arm is unaffected.

This test runs against a dedicated pgserver carrying BOTH the app (alembic)
schema and the real procrastinate schema so the anti-join is exercised for real.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

procrastinate = pytest.importorskip("procrastinate")

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def fix1_pg(module_db):
    """module_db (uuidv7 shim already applied) with the real procrastinate schema
    applied on top, so retry-extracts' anti-join runs for real."""
    import procrastinate as _proc
    import psycopg

    schema_sql = (
        Path(os.path.dirname(_proc.__file__)) / "sql" / "schema.sql"
    ).read_text()
    with psycopg.connect(module_db.get_uri(), autocommit=True) as conn:
        conn.execute(schema_sql)
    return module_db


@pytest.fixture
async def wired(fix1_pg, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from filearr import db as db_mod
    from filearr.api import libraries as lib_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    uri = fix1_pg.get_uri()
    monkeypatch.setenv("FILEARR_DATABASE_URL", uri)
    get_settings.cache_clear()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_async_engine(_psycopg3(uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM procrastinate_jobs"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    deferred: list[list[str]] = []

    async def _fake_defer(ids):
        deferred.append(list(ids))

    monkeypatch.setattr(lib_mod, "defer_extract", _fake_defer)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    yield {"app": app, "maker": maker, "deferred": deferred, "engine": engine}
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mk_lib(maker, name="lib"):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name=name, root_path="/d")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path, *, metadata=None, quick_hash=None):
    from filearr.models import Item, MediaType

    async with maker() as s:
        item = Item(
            library_id=library_id,
            media_type=MediaType.audio,
            status="active",
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp3",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=metadata or {},
            quick_hash=quick_hash,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _seed_pending_job(engine, item_id, status="doing"):
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES ('extract', 'filearr.tasks.extract.extract_item', "
                "CAST(:args AS jsonb), CAST(:st AS procrastinate_job_status))"
            ),
            {"args": json.dumps({"item_id": item_id}), "st": status},
        )


async def test_pending_job_suppresses_null_hash_redefer(wired):
    """A never-hashed item WITH a pending extract job must NOT be re-deferred."""
    maker, engine = wired["maker"], wired["engine"]
    lib = await _mk_lib(maker)
    item = await _mk_item(maker, lib, "a.mp3", quick_hash=None)
    await _seed_pending_job(engine, item, status="doing")

    async with _client(wired["app"]) as c:
        r = await c.post(f"/api/v1/libraries/{lib}/retry-extracts")
        assert r.status_code == 200, r.text
        assert r.json()["retried"] == 0

    assert wired["deferred"] in ([], [[]]) or all(
        item not in batch for batch in wired["deferred"]
    )


async def test_no_pending_job_redefers_null_hash(wired):
    """A never-hashed item with NO pending job IS re-deferred (self-heal intact)."""
    maker = wired["maker"]
    lib = await _mk_lib(maker)
    item = await _mk_item(maker, lib, "b.mp3", quick_hash=None)

    async with _client(wired["app"]) as c:
        r = await c.post(f"/api/v1/libraries/{lib}/retry-extracts")
        assert r.status_code == 200, r.text
        assert r.json()["retried"] == 1

    deferred_ids = {i for batch in wired["deferred"] for i in batch}
    assert deferred_ids == {item}


async def test_error_arm_ignores_pending_job(wired):
    """The _extract_error arm always requeues, even with a pending job present."""
    maker, engine = wired["maker"], wired["engine"]
    lib = await _mk_lib(maker)
    item = await _mk_item(
        maker, lib, "c.mp3", metadata={"_extract_error": "boom"}, quick_hash="h1"
    )
    await _seed_pending_job(engine, item, status="todo")

    async with _client(wired["app"]) as c:
        r = await c.post(f"/api/v1/libraries/{lib}/retry-extracts")
        assert r.status_code == 200, r.text
        assert r.json()["retried"] == 1

    deferred_ids = {i for batch in wired["deferred"] for i in batch}
    assert deferred_ids == {item}
