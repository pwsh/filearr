"""Visual filter builder backend — POST /query/preview + GET /query/keys.

The preview endpoint reuses the EXACT custom-report machinery (parse -> translate
-> scoped SELECT), so these tests focus on the delta the builder adds:

* structured 422s (parse position, translation message, fuzzy ``unsupported``);
* the fixed preview column set + ``item_id`` on every row (opens ItemDetail);
* the ``limit`` cap (<=50) and the capped total count (``total`` <= 10k with a
  ``total_capped`` flag) — both cheap by construction;
* RBAC row scoping IDENTICAL to reports (a scoped principal's preview + count
  never surface a denied row) — reusing the P6-T4 auth-on fixture pattern;
* the ``/keys`` value-picker vocabulary (profile meta keys + custom-field names).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, grant_cache, rbac
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    CustomField,
    Item,
    ItemStatus,
    Library,
    PathGrant,
    Principal,
    User,
)

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Auth-OFF fixture (unrestricted) — most preview behaviour                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM custom_fields"))
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


async def _mk_item(maker, library_id, rel_path, **kw):
    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category=kw.get("file_category", "other"),
            file_group=kw.get("file_group"),
            status=kw.get("status", ItemStatus.active),
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension=kw.get("extension", "bin"),
            size=kw.get("size", 100),
            mtime=kw.get("mtime") or datetime.now(UTC),
            metadata_=kw.get("metadata") or {},
            user_metadata=kw.get("user_metadata") or {},
            external_ids={},
            title=kw.get("title"),
            tags=kw.get("tags") or [],
            first_seen=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _preview(client, query, **kw):
    body = {"query": query, **kw}
    return await client.post("/api/v1/query/preview", json=body)


# --------------------------------------------------------------------------- #
# Happy path + column set + item_id                                           #
# --------------------------------------------------------------------------- #
async def test_preview_returns_fixed_columns_and_item_id(env):
    client, maker = env
    lib = await _mk_lib(maker)
    iid = await _mk_item(maker, lib, "Movies/a.mkv", file_category="video", file_group="video",
                         extension="mkv", size=5_000_000)
    await _mk_item(maker, lib, "Music/b.mp3", file_category="audio", file_group="audio-lossy")

    r = await _preview(client, "kind:video")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == [
        "filename", "library", "rel_path", "file_category", "file_group", "size", "mtime",
    ]
    assert body["count"] == 1
    assert body["total"] == 1
    assert body["total_capped"] is False
    row = body["rows"][0]
    assert row["item_id"] == iid  # opens ItemDetail
    assert row["filename"] == "a.mkv"
    assert row["library"] == "Lib"
    assert row["file_category"] == "video"
    assert row["file_group"] == "video"


async def test_preview_empty_query_matches_all_active(env):
    client, maker = env
    lib = await _mk_lib(maker)
    for i in range(3):
        await _mk_item(maker, lib, f"f{i}.bin")
    r = await _preview(client, "")
    assert r.status_code == 200
    assert r.json()["total"] == 3


# --------------------------------------------------------------------------- #
# Structured errors                                                            #
# --------------------------------------------------------------------------- #
async def test_preview_parse_error_is_422_with_position(env):
    client, _maker = env
    r = await _preview(client, "size:>>5")
    assert r.status_code == 422
    val = r.json()["detail"]["validation"][0]
    assert val["error"] == "parse_error"
    assert "position" in val


async def test_preview_fuzzy_is_unsupported_translation_error(env):
    client, _maker = env
    r = await _preview(client, "~documentaru")
    assert r.status_code == 422
    val = r.json()["detail"]["validation"][0]
    assert val["error"] == "translation_error"
    assert val["unsupported"] == ["documentaru"]


async def test_preview_unknown_cf_is_422(env):
    client, _maker = env
    r = await _preview(client, "cf.nope:>1")
    assert r.status_code == 422
    assert r.json()["detail"]["validation"][0]["error"] == "translation_error"


# --------------------------------------------------------------------------- #
# Limit cap + count cap                                                        #
# --------------------------------------------------------------------------- #
async def test_preview_limit_cap_rejects_over_50(env):
    client, _maker = env
    r = await _preview(client, "", limit=51)
    assert r.status_code == 422


async def test_preview_limit_and_offset_page(env):
    client, maker = env
    lib = await _mk_lib(maker)
    for i in range(5):
        await _mk_item(maker, lib, f"f{i:02d}.bin")
    r = await _preview(client, "", limit=2, offset=0)
    b = r.json()
    assert b["count"] == 2
    assert b["has_more"] is True
    assert b["total"] == 5
    r2 = await _preview(client, "", limit=2, offset=4)
    assert r2.json()["count"] == 1
    assert r2.json()["has_more"] is False


async def test_preview_count_cap_flags_plus(env, monkeypatch):
    client, maker = env
    # Shrink the count ceiling so the test seeds few rows but still trips the cap.
    import filearr.api.query as query_mod
    monkeypatch.setattr(query_mod, "COUNT_CAP", 3)
    lib = await _mk_lib(maker)
    for i in range(5):
        await _mk_item(maker, lib, f"f{i:02d}.bin")
    r = await _preview(client, "", limit=2)
    b = r.json()
    assert b["total"] == 3
    assert b["total_capped"] is True


# --------------------------------------------------------------------------- #
# /keys value-picker vocabulary                                               #
# --------------------------------------------------------------------------- #
async def test_keys_endpoint_lists_profile_and_cf_keys(env):
    client, maker = env
    async with maker() as s:
        s.add(CustomField(name="rating", label="Rating", data_type="integer"))
        await s.commit()
    r = await client.get("/api/v1/query/keys")
    assert r.status_code == 200
    b = r.json()
    meta_keys = {k["key"] for k in b["meta_keys"]}
    assert "height" in meta_keys  # from the image/video profiles
    assert "duration" in meta_keys
    assert {c["name"] for c in b["custom_fields"]} == {"rating"}
    assert "video" in b["kinds"] and "audio" in b["kinds"]
    assert b["source"] == "metadata_profiles+custom_fields"
    # A profile meta key carries its media types + type hint.
    height = next(k for k in b["meta_keys"] if k["key"] == "height")
    assert height["data_type"] == "integer"
    assert "image" in height["file_categories"]


# --------------------------------------------------------------------------- #
# Auth-ON fixture — RBAC row scoping identical to reports (P6-T4)              #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def rbac_env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "path_grants",
            "sessions",
            "items",
            "libraries",
            "users",
            "principals",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    grant_cache._cache.clear()
    grant_cache.bump_generation()
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
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


async def _mk_user(maker, username, role="user", password="pw-123456"):
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username=username.lower(),
                password_hash=authx.hash_password(password),
                auth_provider="local",
            )
        )
        await s.commit()
        return p.id


def _scoped_item(lib, rel):
    return Item(
        library_id=lib.id,
        file_category="video", file_group="video",
        path=f"/data/{lib.name}/{rel}",
        rel_path=rel,
        filename=rel.rsplit("/", 1)[-1],
        extension=rel.rsplit(".", 1)[-1],
        size=random.randint(1, 10_000),
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        first_seen=datetime.now(UTC),
        path_scope=rbac.path_to_ltree(rel, library_id=lib.id),
    )


async def _grant(maker, subject_id, library_id, rel, action="search_metadata"):
    async with maker() as s:
        scope = rbac.path_to_ltree(rel, library_id=library_id)
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=subject_id,
                library_id=library_id,
                scope=scope,
                action=action,
                effect="allow",
            )
        )
        await s.commit()
    grant_cache.bump_generation()


async def _login(c, username, password="pw-123456"):
    r = await c.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return r


async def test_preview_rbac_scopes_rows_and_count(rbac_env):
    client, maker = rbac_env
    async with maker() as s:
        lib = Library(name="movies", root_path="/data/movies")
        s.add(lib)
        await s.flush()
        lib_id = lib.id
        s.add_all([
            _scoped_item(lib, "a/one.mkv"),
            _scoped_item(lib, "a/two.mkv"),
            _scoped_item(lib, "b/three.mkv"),
        ])
        await s.commit()

    uid = await _mk_user(maker, "alice")
    # Grant only subtree "a" — the "b/three.mkv" row must be invisible.
    await _grant(maker, uid, lib_id, "a")
    await _login(client, "alice")

    r = await client.post(
        "/api/v1/query/preview", json={"query": "kind:video", "limit": 50}
    )
    assert r.status_code == 200, r.text
    b = r.json()
    rels = {row["rel_path"] for row in b["rows"]}
    assert rels == {"a/one.mkv", "a/two.mkv"}
    assert b["total"] == 2  # count reflects scope, denied row excluded


async def test_preview_requires_auth_when_enabled(rbac_env):
    client, _maker = rbac_env
    r = await client.post("/api/v1/query/preview", json={"query": ""})
    assert r.status_code == 401
