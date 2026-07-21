"""P12-T13 — agent-plane thumbnail upload (POST /agents/{id}/thumbs).

Covers the write-if-absent small-blob contract: auth + agents-enabled gate, item
resolution UNDER the uploading agent's library (ownership by construction),
size/magic validation, the content-addressed key cross-check, idempotency (a
re-upload is a 200 no-op), and the serve path returning the correct sniffed
Content-Type for an agent-generated JPEG. Mirrors test_agent_staging's harness.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import thumbs as th
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, Item, Library, ThumbnailManifest

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _jpeg(w: int = 40, h: int = 30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 180, 90)).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM thumbnail_manifest"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker, *, rel_path: str = "x.jpg", content_hash: str | None = "ch123"):
    """Seed an active agent + an AGENT-OWNED library + a hashed image item under
    it. Returns (agent_id, item_id, fingerprint, library_ref)."""
    fp = "FP:" + uuid.uuid4().hex
    library_ref = "/data/media"
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path=library_ref,
            source_agent_id=agent.id,
            agent_library_ref=library_ref,
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="image", file_group="raster-photo",
            path=f"{library_ref}/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            size=1,
            mtime=datetime.now(UTC),
            content_hash=content_hash,
            source_agent_id=agent.id,
        )
        s.add(item)
        await s.commit()
        return agent.id, item.id, fp, library_ref


@pytest.fixture
async def client(db_maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    # The FIX-11 low-space guard uses os.statvfs (POSIX-only; absent on the Windows
    # dev host — a documented dev-windows.md limitation). It is exercised on its own
    # elsewhere; no-op it here so the store path is portable in CI on any OS.
    monkeypatch.setattr("filearr.diskguard.guard_write", lambda *a, **k: {})
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


def _hdrs(fp, library_ref, rel_path, tier, *, key=None, extra=None):
    import urllib.parse

    h = {
        "Authorization": f"Bearer {fp}",
        "Content-Type": "image/jpeg",
        "X-Filearr-Library-Ref": urllib.parse.quote_plus(library_ref),
        "X-Filearr-Rel-Path": urllib.parse.quote_plus(rel_path),
        "X-Filearr-Thumb-Tier": tier,
        "X-Filearr-Thumb-Width": "40",
        "X-Filearr-Thumb-Height": "30",
    }
    if key is not None:
        h["X-Filearr-Thumb-Key"] = key
    if extra:
        h.update(extra)
    return h


def _url(agent_id):
    return f"/api/v1/agents/{agent_id}/thumbs"


async def test_upload_creates_then_noop(client):
    c, maker, settings = client
    agent_id, item_id, fp, ref = await _seed(maker)
    key = th.cache_key("ch123", settings.thumbnail_generator_version, th.TIER_GRID)
    body = _jpeg()

    r = await c.post(_url(agent_id), content=body, headers=_hdrs(fp, ref, "x.jpg", "grid", key=key))
    assert r.status_code == 201, r.text
    assert r.json()["cache_key"] == key

    # The file landed at the content-addressed path, byte-identical.
    path = th.abs_path(settings, key)
    assert Path(path).read_bytes() == body  # noqa: ASYNC240 - test asserting local cache write
    async with maker() as s:
        row = (
            await s.execute(
                select(ThumbnailManifest).where(ThumbnailManifest.item_id == item_id)
            )
        ).scalar_one()
        assert row.source == "agent"
        assert row.cache_key == key
        assert row.bytes == len(body)
        assert row.width == 40 and row.height == 30

    # Idempotent: a second upload of the same key is a 200 no-op.
    r2 = await c.post(
        _url(agent_id), content=body, headers=_hdrs(fp, ref, "x.jpg", "grid", key=key)
    )
    assert r2.status_code == 200, r2.text


async def test_rel_path_percent_decoded(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker, rel_path="My Movie.jpg")
    key = th.cache_key("ch123", settings.thumbnail_generator_version, th.TIER_GRID)
    r = await c.post(
        _url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "My Movie.jpg", "grid", key=key)
    )
    assert r.status_code == 201, r.text


async def test_unknown_library_or_item_404(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    # Unknown library_ref for this agent.
    r = await c.post(_url(agent_id), content=_jpeg(), headers=_hdrs(fp, "/other", "x.jpg", "grid"))
    assert r.status_code == 404
    # Known library, unknown rel_path.
    r2 = await c.post(
        _url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "missing.jpg", "grid")
    )
    assert r2.status_code == 404


async def test_foreign_agent_cannot_write(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    # A second, unrelated agent tries to push a thumb for the first agent's item.
    async with maker() as s:
        other_fp = "FP:" + uuid.uuid4().hex
        other = Agent(name="evil", hostname="evil", platform="linux", cert_fingerprint=other_fp)
        s.add(other)
        await s.commit()
        other_id = other.id
    r = await c.post(_url(other_id), content=_jpeg(), headers=_hdrs(other_fp, ref, "x.jpg", "grid"))
    # The library resolves only under the OWNING agent -> an indistinguishable 404.
    assert r.status_code == 404


async def test_bad_tier_and_missing_headers(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    r = await c.post(_url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "x.jpg", "huge"))
    assert r.status_code == 422
    h = _hdrs(fp, ref, "x.jpg", "grid")
    del h["X-Filearr-Rel-Path"]
    r2 = await c.post(_url(agent_id), content=_jpeg(), headers=h)
    assert r2.status_code == 422


async def test_non_image_body_415(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    r = await c.post(
        _url(agent_id),
        content=b"this is definitely not an image",
        headers=_hdrs(fp, ref, "x.jpg", "grid"),
    )
    assert r.status_code == 415


async def test_oversize_body_413(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    big = b"\xff\xd8\xff" + b"\x00" * (settings.thumbnail_agent_max_bytes + 10)
    r = await c.post(_url(agent_id), content=big, headers=_hdrs(fp, ref, "x.jpg", "grid"))
    assert r.status_code == 413


async def test_key_mismatch_409(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    r = await c.post(
        _url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "x.jpg", "grid", key="deadbeef" * 4)
    )
    assert r.status_code == 409


async def test_unhashed_item_409(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker, content_hash=None)
    r = await c.post(_url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "x.jpg", "grid"))
    assert r.status_code == 409


async def test_bad_bearer_401(client):
    c, maker, settings = client
    agent_id, _item, _fp, ref = await _seed(maker)
    r = await c.post(
        _url(agent_id), content=_jpeg(), headers=_hdrs("FP:wrong", ref, "x.jpg", "grid")
    )
    assert r.status_code == 401


async def test_agents_disabled_404(client):
    c, maker, settings = client
    agent_id, _item, fp, ref = await _seed(maker)
    settings.agents_enabled = False
    r = await c.post(_url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "x.jpg", "grid"))
    assert r.status_code == 404


async def test_serve_sniffs_jpeg_content_type(client):
    """An agent-generated JPEG (stored under the .webp cache name) serves with the
    correct sniffed Content-Type, not the hardcoded image/webp."""
    c, maker, settings = client
    agent_id, item_id, fp, ref = await _seed(maker)
    key = th.cache_key("ch123", settings.thumbnail_generator_version, th.TIER_GRID)
    up = await c.post(
        _url(agent_id), content=_jpeg(), headers=_hdrs(fp, ref, "x.jpg", "grid", key=key)
    )
    assert up.status_code == 201

    # auth is disabled in the fixture -> serve is unrestricted.
    r = await c.get(f"/api/v1/items/{item_id}/thumb?tier=grid")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
