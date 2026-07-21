"""W8-A — File Extension Similarity Taxonomy foundation.

Covers, against the migrated Postgres (same harness as test_replication_p5t4):

* **schema + seed via migration** — the four taxonomy tables exist and are seeded
  (9 categories, 37 groups, >800 extensions, taxonomy_state.version=1).
* **runtime service** — ``filearr.taxonomy.load/detect`` classifies from the DB
  (compound ``tar.*``, unknown -> other, dotfiles), ``category_extractor`` mirrors
  today's routing, ``tree`` returns the frozen shape.
* **admin CRUD** — create/update/delete categories & groups, add/remove/reparent
  extensions; every edit bumps ``taxonomy_state.version`` and invalidates the
  in-process cache (a fresh ``detect`` sees the edit); category-delete is refused
  while it has groups; ext charset is validated.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import taxonomy
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    taxonomy.invalidate()  # start from a clean process-global cache
    engine = create_async_engine(_psycopg3(pg_uri))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()
    taxonomy.invalidate()


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Schema + seed via migration                                                  #
# --------------------------------------------------------------------------- #
async def test_migration_seeds_taxonomy(maker):
    async with maker() as s:
        assert (await s.execute(text("SELECT count(*) FROM file_categories"))).scalar_one() == 9
        assert (await s.execute(text("SELECT count(*) FROM file_groups"))).scalar_one() == 37
        assert (
            await s.execute(text("SELECT count(*) FROM file_group_extensions"))
        ).scalar_one() > 800
        assert (
            await s.execute(text("SELECT version FROM taxonomy_state WHERE id = 1"))
        ).scalar_one() == 1
        # every seeded group's category_id resolves (FK integrity)
        orphans = (
            await s.execute(
                text(
                    "SELECT count(*) FROM file_groups g "
                    "LEFT JOIN file_categories c ON g.category_id = c.id "
                    "WHERE c.id IS NULL"
                )
            )
        ).scalar_one()
        assert orphans == 0
        # the image category carries its extractor
        ex = (
            await s.execute(
                text("SELECT extractor FROM file_categories WHERE key = 'image'")
            )
        ).scalar_one()
        assert ex == "image"


# --------------------------------------------------------------------------- #
# Runtime service (DB-driven)                                                  #
# --------------------------------------------------------------------------- #
async def test_service_detect_from_db(maker):
    async with maker() as s:
        tax = await taxonomy.load(s)
        assert tax.version == 1
        assert tax.detect("holiday.jpg") == ("image", "raster-photo")
        assert tax.detect("IMG.CR2") == ("image", "raw-photo")   # case-insensitive
        assert tax.detect("song.flac") == ("audio", "audio-lossless")
        assert tax.detect("movie.mkv") == ("video", "video")
        assert tax.detect("subs.srt") == ("video", "subtitle")
        assert tax.detect("main.py") == ("development", "source-code")
        assert tax.detect("archive.zip") == ("archive", "archive")
        assert tax.detect("backup.tar.gz") == ("archive", "archive")   # compound
        assert tax.detect("mystery.zzzzz") == ("other", "other")       # unknown
        assert tax.detect(".bashrc") == ("other", "other")             # dotfile
        # the convenience wrapper agrees with the snapshot
        assert await taxonomy.detect(s, "movie.mkv") == ("video", "video")


async def test_service_category_extractor(maker):
    async with maker() as s:
        assert await taxonomy.category_extractor(s, "image") == "image"
        assert await taxonomy.category_extractor(s, "audio") == "audio"
        assert await taxonomy.category_extractor(s, "video") == "video"
        assert await taxonomy.category_extractor(s, "document") == "document"
        assert await taxonomy.category_extractor(s, "three-d-cad") == "model3d"
        assert await taxonomy.category_extractor(s, "development") is None
        assert await taxonomy.category_extractor(s, "archive") is None
        assert await taxonomy.category_extractor(s, "system") is None
        assert await taxonomy.category_extractor(s, "nope") is None


async def test_tree_shape(client):
    r = await client.get("/api/v1/taxonomy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    tree = body["tree"]
    assert isinstance(tree, list) and len(tree) == 9
    entry = tree[0]
    assert set(entry) == {"category", "groups"}
    cat = entry["category"]
    assert set(cat) == {"key", "label", "description", "extractor", "sort_order", "is_builtin"}
    image = next(e for e in tree if e["category"]["key"] == "image")
    assert image["category"]["extractor"] == "image"
    raster = next(g for g in image["groups"] if g["key"] == "raster-photo")
    assert set(raster) == {"key", "label", "description", "sort_order", "is_builtin", "extensions"}
    assert "jpg" in raster["extensions"]
    # a rescue category with a null extractor
    archive = next(e for e in tree if e["category"]["key"] == "archive")
    assert archive["category"]["extractor"] is None


# --------------------------------------------------------------------------- #
# Admin CRUD + version bump + cache invalidation + ext reparent                #
# --------------------------------------------------------------------------- #
async def _version(client) -> int:
    return (await client.get("/api/v1/taxonomy")).json()["version"]


async def test_crud_lifecycle_and_cache_invalidation(client, maker):
    v0 = await _version(client)

    # create a category (no extractor)
    r = await client.post(
        "/api/v1/taxonomy/categories",
        json={"key": "w8cat", "label": "W8 Cat", "description": "test", "extractor": None},
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_builtin"] is False
    v1 = await _version(client)
    assert v1 > v0  # version bumped

    # create a group under it
    r = await client.post(
        "/api/v1/taxonomy/groups",
        json={"key": "w8grp", "label": "W8 Grp", "category": "w8cat"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["category"] == "w8cat"

    # add a brand-new extension
    r = await client.post(
        "/api/v1/taxonomy/groups/w8grp/extensions", json={"ext": "w8ext"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["previous_group"] is None

    # cache invalidation: a FRESH detect (new session) sees the edit
    async with maker() as s:
        assert await taxonomy.detect(s, "file.w8ext") == ("w8cat", "w8grp")

    # reparent the ext to a second group -> returns the prior group
    r = await client.post(
        "/api/v1/taxonomy/groups", json={"key": "w8grp2", "label": "W8 Grp 2", "category": "w8cat"}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/taxonomy/groups/w8grp2/extensions", json={"ext": "w8ext"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["previous_group"] == "w8grp"
    async with maker() as s:
        assert await taxonomy.detect(s, "file.w8ext") == ("w8cat", "w8grp2")

    # update the category's extractor + a group's label
    r = await client.patch(
        "/api/v1/taxonomy/categories/w8cat", json={"extractor": "document"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["extractor"] == "document"
    async with maker() as s:
        assert await taxonomy.category_extractor(s, "w8cat") == "document"

    # remove the extension
    r = await client.request(
        "DELETE", "/api/v1/taxonomy/extensions/w8ext"
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        assert await taxonomy.detect(s, "file.w8ext") == ("other", "other")

    # delete groups (cascades any remaining exts), then the now-empty category
    assert (await client.request("DELETE", "/api/v1/taxonomy/groups/w8grp")).status_code == 200
    assert (await client.request("DELETE", "/api/v1/taxonomy/groups/w8grp2")).status_code == 200
    assert (
        await client.request("DELETE", "/api/v1/taxonomy/categories/w8cat")
    ).status_code == 200

    # seed intact after cleanup
    async with maker() as s:
        assert await taxonomy.detect(s, "movie.mkv") == ("video", "video")


async def test_category_delete_refused_while_it_has_groups(client):
    # a builtin category still parents groups -> 409, not deleted
    r = await client.request("DELETE", "/api/v1/taxonomy/categories/image")
    assert r.status_code == 409, r.text
    # still present
    tree = (await client.get("/api/v1/taxonomy")).json()["tree"]
    assert any(e["category"]["key"] == "image" for e in tree)


async def test_ext_charset_validation(client):
    # Genuinely-invalid inputs are rejected 422 (no mutation). Case is NORMALIZED
    # to lowercase (an operator typing "MP3" is fine), so uppercase is NOT a
    # rejection case — only dots / spaces / other punctuation are.
    for bad in ["has space", "dot.ext", "sla/sh", "a*b"]:
        r = await client.post(
            "/api/v1/taxonomy/groups/video/extensions", json={"ext": bad}
        )
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"


async def test_invalid_extractor_rejected(client):
    r = await client.post(
        "/api/v1/taxonomy/categories",
        json={"key": "w8bad", "label": "x", "extractor": "not-a-pipeline"},
    )
    assert r.status_code == 422, r.text
