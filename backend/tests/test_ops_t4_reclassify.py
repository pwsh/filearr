"""OPS-T4 — extension-map expansion, unmapped-report sidecar fix, and the
in-place ``reclassify-extensions`` endpoint.

Layers:
* **pure** — the new ``media_types.detect`` mappings (tif/psd/3gp/mts/...);
* **integration** (pgserver + alembic) — the ``unmapped_extensions`` report now
  excludes linked sidecars (``sidecar_of IS NOT NULL``), and the reclassify pass
  moves stale ``media_type`` rows to the current map, returns per-type counts,
  and defers a bounded index re-sync for exactly the changed ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.media_types import detect
from filearr.models import Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure: extension-map additions                                               #
# --------------------------------------------------------------------------- #
def test_new_image_extensions_map_to_image():
    for name in ("photo.tif", "master.tiff", "layered.psd", "SHOT.PSD"):
        assert detect(name) == "image"


def test_new_video_extensions_map_to_video():
    for name in ("clip.3gp", "clip.3g2", "handycam.mts", "disc.m2ts"):
        assert detect(name) == "video"


def test_deliberately_unmapped_stay_other():
    # W8-B: genuinely unrecognised extensions remain 'other' (visible,
    # presets-excludable). Note exe/dll now classify as file_category 'system'
    # (the taxonomy rescues them from the old catch-all).
    for name in ("game.sc2save", "pack.themepack", "x.tsk", "y.url", "z.download"):
        assert detect(name) == "other"
    for name in ("tool.exe", "lib.dll"):
        assert detect(name) == "system"


def test_extensionless_is_other():
    assert detect("README") == "other"


# --------------------------------------------------------------------------- #
# Integration fixture (real Postgres)                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
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


async def _mk_lib(maker, name="Lib"):
    async with maker() as s:
        lib = Library(name=name, root_path="/data/l")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(
    maker, library_id, rel_path, *, file_category="other", file_group="other",
    status="active", extension="bin", size=100, sidecar_of=None,
):
    async with maker() as s:
        item = Item(
            library_id=library_id, file_category=file_category, file_group=file_group,
            status=status,
            path=f"/data/l/{rel_path}", rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1], extension=extension, size=size,
            mtime=datetime.now(UTC), metadata_={}, user_metadata={},
            external_ids={}, tags=[], sidecar_of=sidecar_of,
        )
        s.add(item)
        await s.commit()
        return str(item.id)


# --------------------------------------------------------------------------- #
# Report fix: unmapped_extensions excludes sidecars + trashed                 #
# --------------------------------------------------------------------------- #
async def test_unmapped_excludes_sidecars_and_trashed(api):
    client, maker = api
    lib = await _mk_lib(maker)
    # A real unmapped file — must appear.
    await _mk_item(maker, lib, "game.sc2save", extension="sc2save", size=10)
    # A primary media item to be the sidecar parent.
    parent = await _mk_item(
        maker, lib, "movie.mkv", file_category="video", file_group="video", extension="mkv"
    )
    # Linked sidecars (media_type=other) — must NOT appear despite being 'other'.
    await _mk_item(maker, lib, "movie.nfo", extension="nfo", size=5, sidecar_of=parent)
    await _mk_item(
        maker, lib, "movie_JRSidecar.xml", extension="xml", size=5, sidecar_of=parent
    )
    # A trashed unmapped file — must NOT appear (status gate).
    await _mk_item(maker, lib, "old.sc2save", extension="sc2save", status="trashed")

    r = await client.get("/api/v1/reports/unmapped_extensions")
    rows = {row["extension"]: row for row in r.json()["rows"]}
    assert set(rows) == {"sc2save"}
    assert rows["sc2save"]["file_count"] == 1  # trashed one excluded


# --------------------------------------------------------------------------- #
# reclassify-extensions endpoint                                              #
# --------------------------------------------------------------------------- #
async def test_reclassify_updates_media_type_and_resyncs(api, monkeypatch):
    client, maker = api
    from filearr import worker

    deferred: list[list[str]] = []

    async def _fake_defer(item_ids):
        deferred.append(list(item_ids))

    monkeypatch.setattr(worker, "defer_index_sync", _fake_defer)

    lib = await _mk_lib(maker)
    # Stale rows recorded as 'other' at last scan, now mapped by the taxonomy.
    tif = await _mk_item(maker, lib, "a.tif", extension="tif")
    psd = await _mk_item(maker, lib, "b.psd", extension="psd")
    tgp = await _mk_item(maker, lib, "c.3gp", extension="3gp")
    # A genuinely unmappable row — must stay 'other', untouched.
    keep = await _mk_item(maker, lib, "d.sc2save", extension="sc2save")
    # An already-correct row — must NOT be counted/resynced.
    ok = await _mk_item(
        maker, lib, "e.mkv", extension="mkv", file_category="video", file_group="video"
    )

    r = await client.post("/api/v1/system/reclassify-extensions")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == 3
    assert body["by_category"] == {"image": 2, "video": 1}

    async with maker() as s:
        rows = {
            str(i.id): i.file_category
            for i in (await s.execute(select(Item))).scalars().all()
        }
    assert rows[tif] == "image"
    assert rows[psd] == "image"
    assert rows[tgp] == "video"
    assert rows[keep] == "other"
    assert rows[ok] == "video"

    # Exactly the 3 changed ids were deferred for index re-sync (bounded batches).
    synced = {i for batch in deferred for i in batch}
    assert synced == {tif, psd, tgp}


async def test_reclassify_demotes_unmapped_to_other(api, monkeypatch):
    client, maker = api
    from filearr import worker

    monkeypatch.setattr(worker, "defer_index_sync", lambda item_ids: _noop())

    lib = await _mk_lib(maker)
    # A row misclassified as image but whose extension is no longer mapped, and an
    # extensionless row mislabeled video — both must be demoted to 'other'.
    bad1 = await _mk_item(maker, lib, "x.junkext", extension="junkext",
                          file_category="image", file_group="raster-photo")
    bad2 = await _mk_item(maker, lib, "NOEXT", extension=None,
                          file_category="video", file_group="video")
    r = await client.post("/api/v1/system/reclassify-extensions")
    assert r.status_code == 200
    assert r.json()["by_category"].get("other") == 2

    async with maker() as s:
        rows = {
            str(i.id): i.file_category
            for i in (await s.execute(select(Item))).scalars().all()
        }
    assert rows[bad1] == "other"
    assert rows[bad2] == "other"


async def _noop():
    return None
