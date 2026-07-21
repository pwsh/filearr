"""P10-T13: transfer API — validation order + scope gate (post-graduation).

The transfer router is now LIVE (see test_transfers_p10t13.py for the full
retrieve behaviour). This file keeps the input-validation-order and coarse-scope
contract: input validation (404 unknown item, 422 bad params, 422 malformed uuid)
runs and the coarse scope is enforced NOW; a valid request then reaches the live
handler (a non-agent-hosted seed item → 422; an unknown transfer id → 404). Uses a
real Postgres (alembic head) like the other API tests.
"""

from __future__ import annotations

import uuid
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
async def ctx(pg_uri, monkeypatch):
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
        yield c, maker, monkeypatch
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_item(maker) -> str:
    async with maker() as s:
        lib = (
            await s.execute(
                text(
                    "INSERT INTO libraries (name, root_path) VALUES ('L','/data/l') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        item = (
            await s.execute(
                text(
                    "INSERT INTO items (library_id, file_category, status, path, rel_path, "
                    "filename, size, mtime) VALUES (:lib, 'video', 'active', "
                    "'/data/l/x.mkv', 'x.mkv', 'x.mkv', 10, now()) RETURNING id"
                ),
                {"lib": lib},
            )
        ).scalar_one()
        await s.commit()
        return str(item)


# --- POST /items/{id}/transfer --------------------------------------------- #


async def test_initiate_unknown_item_404(ctx):
    client, _, _ = ctx
    r = await client.post(f"/api/v1/items/{uuid.uuid4()}/transfer", json={})
    assert r.status_code == 404, r.text


async def test_initiate_bad_body_422_before_501(ctx):
    client, maker, _ = ctx
    item_id = await _seed_item(maker)
    # Non-positive rate must 422 (contract validation), not 501.
    r = await client.post(
        f"/api/v1/items/{item_id}/transfer", json={"max_bytes_per_sec": 0}
    )
    assert r.status_code == 422, r.text


async def test_initiate_extra_field_422(ctx):
    client, maker, _ = ctx
    item_id = await _seed_item(maker)
    r = await client.post(f"/api/v1/items/{item_id}/transfer", json={"bogus": 1})
    assert r.status_code == 422, r.text


async def test_initiate_malformed_item_uuid_422(ctx):
    client, _, _ = ctx
    r = await client.post("/api/v1/items/not-a-uuid/transfer", json={})
    assert r.status_code == 422, r.text


async def test_initiate_valid_but_non_agent_hosted_422(ctx):
    # A validated request on a centrally-scanned (non-agent-hosted) item reaches
    # the live handler and 422s (it has local bytes, needs no transfer).
    client, maker, _ = ctx
    item_id = await _seed_item(maker)
    r = await client.post(f"/api/v1/items/{item_id}/transfer", json={})
    assert r.status_code == 422, r.text


async def test_initiate_valid_body_accepted_then_422(ctx):
    client, maker, _ = ctx
    item_id = await _seed_item(maker)
    r = await client.post(
        f"/api/v1/items/{item_id}/transfer",
        json={"verify_hash": False, "max_bytes_per_sec": 1048576},
    )
    assert r.status_code == 422, r.text


# --- GET/DELETE /transfers/{id} -------------------------------------------- #


async def test_status_unknown_transfer_404(ctx):
    client, _, _ = ctx
    r = await client.get(f"/api/v1/transfers/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_status_malformed_uuid_422(ctx):
    client, _, _ = ctx
    r = await client.get("/api/v1/transfers/not-a-uuid")
    assert r.status_code == 422


async def test_download_unknown_transfer_404(ctx):
    client, _, _ = ctx
    r = await client.get(f"/api/v1/transfers/{uuid.uuid4()}/download")
    assert r.status_code == 404


async def test_cancel_unknown_transfer_404(ctx):
    client, _, _ = ctx
    r = await client.delete(f"/api/v1/transfers/{uuid.uuid4()}")
    assert r.status_code == 404


# --- scope gate (auth ON) --------------------------------------------------- #


async def test_write_scope_enforced_on_initiate(ctx):
    client, maker, monkeypatch = ctx
    item_id = await _seed_item(maker)
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    # No bearer token -> 401 before any 501/validation.
    r = await client.post(f"/api/v1/items/{item_id}/transfer", json={})
    assert r.status_code == 401, r.text


async def test_read_scope_enforced_on_status(ctx):
    client, _, monkeypatch = ctx
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    r = await client.get(f"/api/v1/transfers/{uuid.uuid4()}")
    assert r.status_code == 401, r.text


async def test_write_scope_enforced_on_download(ctx):
    client, _, monkeypatch = ctx
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    r = await client.get(f"/api/v1/transfers/{uuid.uuid4()}/download")
    assert r.status_code == 401, r.text
