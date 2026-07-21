"""P3-T11 GPS default-hidden gate + P3-T6 OCR-into-body projection.

Two layers of the gate are covered:
  * PROJECTION (search.build_doc): GPS absent unless expose_gps=True.
  * API RESPONSE (GET /items/{id}): server strips GPS from ``metadata`` unless the
    owning library's expose_gps=True — so the Raw tab never receives it.
Plus the OCR text joining the searchable body_text field, and a Dockerfile check
that the runtime binaries are declared.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import search as search_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, ItemStatus, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _make_item(meta):
    return Item(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="image", file_group="raster-photo",
        path="/data/p.jpg",
        rel_path="p.jpg",
        filename="p.jpg",
        extension="jpg",
        size=1,
        mtime=datetime.now(UTC),
        metadata_=meta,
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
        sidecar_of=None,
    )


_EXIF_META = {
    "exif.camera_make": "Canon",
    "exif.gps_latitude": 37.77,
    "exif.gps_longitude": -122.41,
}


# --- projection gate (build_doc) -------------------------------------------


def test_build_doc_hides_gps_by_default():
    doc = search_mod.build_doc(_make_item(dict(_EXIF_META)))
    assert doc["exif.camera_make"] == "Canon"
    assert "exif.gps_latitude" not in doc
    assert "exif.gps_longitude" not in doc


def test_build_doc_exposes_gps_when_opted_in():
    doc = search_mod.build_doc(_make_item(dict(_EXIF_META)), expose_gps=True)
    assert doc["exif.gps_latitude"] == 37.77
    assert doc["exif.gps_longitude"] == -122.41
    assert doc["exif.camera_make"] == "Canon"


def test_build_doc_joins_ocr_text_into_body():
    item = _make_item({"body_text": "native words", "ocr_text": "scanned words"})
    doc = search_mod.build_doc(item)
    assert "native words" in doc["body_text"]
    assert "scanned words" in doc["body_text"]


def test_build_doc_ocr_only_body():
    item = _make_item({"ocr_text": "only ocr"})
    doc = search_mod.build_doc(item)
    assert doc["body_text"] == "only ocr"


# --- Dockerfile declares the runtime binaries ------------------------------


def test_dockerfile_declares_ocr_exif_binaries():
    df = (BACKEND_DIR.parent / "Dockerfile").read_text()
    assert "tesseract-ocr" in df
    assert "poppler-utils" in df
    assert "libimage-exiftool-perl" in df


# --- API response gate (GET /items/{id}) -----------------------------------


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client_and_maker(pg_uri, monkeypatch):
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


async def _seed(maker, *, expose_gps: bool) -> str:
    async with maker() as s:
        lib = Library(name=f"L{expose_gps}", root_path="/data/l", expose_gps=expose_gps)
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="image", file_group="raster-photo",
            path="/data/l/p.jpg",
            rel_path="p.jpg",
            filename="p.jpg",
            extension=".jpg",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=dict(_EXIF_META),
            user_metadata={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_get_item_strips_gps_when_not_exposed(client_and_maker):
    client, maker = client_and_maker
    item_id = await _seed(maker, expose_gps=False)
    body = (await client.get(f"/api/v1/items/{item_id}")).json()
    md = body["metadata"]
    assert md["exif.camera_make"] == "Canon"  # non-GPS survives
    assert "exif.gps_latitude" not in md
    assert "exif.gps_longitude" not in md


async def test_get_item_exposes_gps_when_opted_in(client_and_maker):
    client, maker = client_and_maker
    item_id = await _seed(maker, expose_gps=True)
    md = (await client.get(f"/api/v1/items/{item_id}")).json()["metadata"]
    assert md["exif.gps_latitude"] == 37.77
    assert md["exif.gps_longitude"] == -122.41


async def test_stored_metadata_untouched_by_gate(client_and_maker):
    """The gate is response-only: the DB row keeps GPS (extracted truth)."""
    client, maker = client_and_maker
    item_id = await _seed(maker, expose_gps=False)
    await client.get(f"/api/v1/items/{item_id}")
    async with maker() as s:
        row = await s.get(Item, uuid.UUID(item_id))
        assert row.metadata_["exif.gps_latitude"] == 37.77
