"""T7 — library create/PATCH API surface for the hash-policy fields.

Drives the real FastAPI app over an in-process ASGI transport (mirrors
test_scan_sse's wiring): create accepts/echoes the fields, defaults to 'auto',
rejects a bad policy or non-positive ceiling with 422, and PATCH updates them.
"""

from __future__ import annotations

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

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client(pg_uri, monkeypatch):
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
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


BASE = "/api/v1/libraries"


async def test_create_defaults_hash_policy_auto(client):
    r = await client.post(BASE, json={"name": "L1", "root_path": "/data/l1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["hash_policy"] == "auto"
    assert body["hash_full_max_bytes"] is None


async def test_create_accepts_full_and_ceiling(client):
    r = await client.post(
        BASE,
        json={
            "name": "L2", "root_path": "/data/l2",
            "hash_policy": "full", "hash_full_max_bytes": 5_000_000_000,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["hash_policy"] == "full"
    assert body["hash_full_max_bytes"] == 5_000_000_000


async def test_create_rejects_unknown_policy(client):
    r = await client.post(
        BASE, json={"name": "Lbad", "root_path": "/d", "hash_policy": "banana"}
    )
    assert r.status_code == 422


async def test_create_rejects_nonpositive_ceiling(client):
    r = await client.post(
        BASE, json={"name": "Lbad2", "root_path": "/d", "hash_full_max_bytes": 0}
    )
    assert r.status_code == 422


async def test_patch_updates_policy_and_ceiling(client):
    created = (await client.post(BASE, json={"name": "L3", "root_path": "/data/l3"})).json()
    lib_id = created["id"]
    r = await client.patch(
        f"{BASE}/{lib_id}",
        json={"hash_policy": "quick_only", "hash_full_max_bytes": 123456},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hash_policy"] == "quick_only"
    assert body["hash_full_max_bytes"] == 123456


async def test_patch_rejects_bad_ceiling(client):
    created = (await client.post(BASE, json={"name": "L4", "root_path": "/data/l4"})).json()
    lib_id = created["id"]
    r = await client.patch(f"{BASE}/{lib_id}", json={"hash_full_max_bytes": -1})
    assert r.status_code == 422
