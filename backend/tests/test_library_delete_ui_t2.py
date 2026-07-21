"""UI-T2 — library hard-delete endpoint.

DELETE /api/v1/libraries/{id}?confirm=<name> (admin scope):
  * 404 unknown library,
  * 422 when confirm != library name,
  * 409 while any ScanRun for it is 'running',
  * else deletes the row (FK CASCADE removes items/scan_runs), prunes Meili by
    explicit id after commit, returns 204.

The Meilisearch projection call (delete_docs) is monkeypatched to record the ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE = "/api/v1/libraries"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired(pg_uri, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from filearr import db as db_mod
    from filearr.api import libraries as lib_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    deleted_batches: list[list[str]] = []

    async def _fake_delete_docs(ids):
        deleted_batches.append(list(ids))

    monkeypatch.setattr(lib_mod, "delete_docs", _fake_delete_docs)

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    yield {"app": app, "maker": maker, "engine": engine, "deleted": deleted_batches}
    app.dependency_overrides.clear()
    await engine.dispose()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mk_lib(maker, name):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name=name, root_path="/d")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path):
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="audio", file_group="audio-lossy",
            status="active",
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp3",
            size=1,
            mtime=datetime.now(UTC),
            metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _mk_scan_run(maker, library_id, status):
    from filearr.models import ScanRun

    async with maker() as s:
        run = ScanRun(library_id=library_id, status=status)
        s.add(run)
        await s.commit()
        return run.id


async def test_delete_unknown_library_404(wired):
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/00000000-0000-0000-0000-000000000000?confirm=x")
        assert r.status_code == 404


async def test_delete_confirm_mismatch_422(wired):
    lib = await _mk_lib(wired["maker"], "Movies")
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Moves")
        assert r.status_code == 422
    # library still present
    async with wired["maker"]() as s:
        from filearr.models import Library

        assert (await s.execute(select(func.count()).select_from(Library))).scalar_one() == 1


async def test_delete_missing_confirm_is_422(wired):
    lib = await _mk_lib(wired["maker"], "Movies")
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}")
        assert r.status_code == 422


async def test_delete_refused_while_scan_running_409(wired):
    lib = await _mk_lib(wired["maker"], "Music")
    await _mk_scan_run(wired["maker"], lib, "running")
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Music")
        assert r.status_code == 409
    async with wired["maker"]() as s:
        from filearr.models import Library

        assert (await s.execute(select(func.count()).select_from(Library))).scalar_one() == 1


async def test_delete_cascades_and_prunes_meili(wired):
    from filearr.models import Item, Library, ScanRun

    maker = wired["maker"]
    lib = await _mk_lib(maker, "Docs")
    id1 = await _mk_item(maker, lib, "a.pdf")
    id2 = await _mk_item(maker, lib, "b.pdf")
    # a finished scan run must not block the delete
    await _mk_scan_run(maker, lib, "completed")

    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Docs")
        assert r.status_code == 204, r.text

    async with maker() as s:
        assert (await s.execute(select(func.count()).select_from(Library))).scalar_one() == 0
        # FK cascade removed items + scan_runs
        assert (await s.execute(select(func.count()).select_from(Item))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(ScanRun))).scalar_one() == 0

    # Meili prune was issued by explicit id for exactly this library's items
    pruned = {i for batch in wired["deleted"] for i in batch}
    assert pruned == {id1, id2}


async def test_delete_no_items_still_204_and_no_meili_error(wired):
    lib = await _mk_lib(wired["maker"], "Empty")
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Empty")
        assert r.status_code == 204
    # a delete batch may be issued only when there are ids; none here
    pruned = {i for batch in wired["deleted"] for i in batch}
    assert pruned == set()
