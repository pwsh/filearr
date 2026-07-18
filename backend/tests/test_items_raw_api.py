"""P4-T11 — the single-item GET exposes EVERY stored column, with ``metadata``
and ``user_metadata`` returned as two separate, UNMERGED objects (never only an
effective overlay). This is the contract the always-available frontend "Raw"
tab depends on."""

from __future__ import annotations

from datetime import UTC, datetime
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
from filearr.models import Item, Library, MediaType

BACKEND_DIR = Path(__file__).resolve().parent.parent


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


# Every stored Item column the Raw tab must be able to surface (response keys).
EXPECTED_COLUMNS = {
    "id", "library_id", "media_type", "status", "path", "rel_path",
    "filename", "extension", "size", "mtime", "quick_hash", "content_hash",
    "title", "year", "first_seen", "last_seen", "deleted_at", "sidecar_of",
    "external_ids", "metadata", "user_metadata", "tags",
    # P4-T7 provenance columns surfaced by the Raw tab.
    "source_agent_id", "replication_seq", "policy_version",
}


async def _seed_item(maker) -> str:
    async with maker() as s:
        lib = Library(name="L", root_path="/data/l", native_prefix="/mnt/user/media")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            path="/data/l/Movies/x.mp4",
            rel_path="Movies/x.mp4",
            filename="x.mp4",
            extension=".mp4",
            size=123,
            mtime=datetime.now(UTC),
            quick_hash="qh",
            content_hash="ch",
            title="X",
            year=2021,
            external_ids={"imdb": "tt1"},
            metadata_={"video_codec": "h264", "year": 2021},
            user_metadata={"video_codec": "h265-override", "note": "mine"},
            tags=["a", "b"],
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_get_item_exposes_every_stored_column(client_and_maker):
    client, maker = client_and_maker
    item_id = await _seed_item(maker)
    r = await client.get(f"/api/v1/items/{item_id}")
    assert r.status_code == 200, r.text
    missing = EXPECTED_COLUMNS - set(r.json())
    assert not missing, f"Raw view missing stored columns: {missing}"


async def test_metadata_columns_returned_separately_unmerged(client_and_maker):
    client, maker = client_and_maker
    item_id = await _seed_item(maker)
    body = (await client.get(f"/api/v1/items/{item_id}")).json()
    # Two SEPARATE objects — never collapsed into an effective overlay.
    assert body["metadata"] == {"video_codec": "h264", "year": 2021}
    assert body["user_metadata"] == {"video_codec": "h265-override", "note": "mine"}
    assert body["metadata"]["video_codec"] == "h264"
    assert body["user_metadata"]["video_codec"] == "h265-override"
    assert "effective_metadata" not in body


async def test_hash_and_lifecycle_columns_present(client_and_maker):
    client, maker = client_and_maker
    item_id = await _seed_item(maker)
    body = (await client.get(f"/api/v1/items/{item_id}")).json()
    assert body["quick_hash"] == "qh"
    assert body["content_hash"] == "ch"
    assert body["deleted_at"] is None
    assert body["sidecar_of"] is None
    assert body["first_seen"] and body["last_seen"]
    assert body["native_path"] == "/mnt/user/media/Movies/x.mp4"
