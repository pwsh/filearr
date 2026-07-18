"""P4-T1 / P4-T2 — metadata profiles seed + read API + extract validation.

Covers:
- seed idempotency (run twice -> no dup rows, version respected, R2 guard);
- ``GET /api/v1/metadata-profiles`` shape (all nine MediaType profiles + a
  single-profile lookup + 404);
- ``extract_item`` validation wiring: a deliberately wrong-typed profile field is
  dropped from metadata_, a compact ``_validation_errors`` list is recorded, the
  valid + unregistered keys are kept, and the job stays green;
- the P4-T5 ``user_metadata_is_object`` CHECK rejects a non-object at the DB layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import MediaType, MetadataProfile
from filearr.profiles import METADATA_PROFILES, PROFILE_VERSION, seed_profiles_to_db

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM metadata_profiles"))
        await conn.execute(text("DELETE FROM custom_fields"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", m)
    yield m
    await engine.dispose()


@pytest.fixture
async def client(maker, monkeypatch):
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


PROFILES = "/api/v1/metadata-profiles"


# --- P4-T1 seed -------------------------------------------------------------


async def test_seed_is_idempotent_and_covers_every_media_type(maker):
    await seed_profiles_to_db(maker)
    await seed_profiles_to_db(maker)  # second run must not duplicate

    async with maker() as s:
        count = (await s.execute(select(func.count()).select_from(MetadataProfile))).scalar()
        rows = (await s.execute(select(MetadataProfile))).scalars().all()

    # one row per MediaType member, no dups.
    assert count == len(MediaType) == len(METADATA_PROFILES)
    by_type = {r.media_type: r for r in rows}
    assert set(by_type) == {mt.value for mt in MediaType}
    for r in rows:
        assert r.version == PROFILE_VERSION
    # audio profile schema mirrors the FieldSpec projection.
    audio = by_type["audio"].schema_
    assert audio["artist"] == {
        "type": "string",
        "required": False,
        "facetable": True,
        "sortable": False,
        "label": "Artist",
    }


async def test_seed_respects_newer_stored_version(maker):
    """R2 guard: a hand-bumped newer row is never downgraded by a re-seed."""
    await seed_profiles_to_db(maker)
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE metadata_profiles SET version = 999, schema = '{\"x\": 1}'::jsonb "
                "WHERE media_type = 'audio'"
            )
        )
        await s.commit()
    await seed_profiles_to_db(maker)  # must NOT clobber the newer row
    async with maker() as s:
        row = (
            await s.execute(
                select(MetadataProfile).where(MetadataProfile.media_type == "audio")
            )
        ).scalar_one()
    assert row.version == 999
    assert row.schema_ == {"x": 1}


# --- P4-T1 read API ---------------------------------------------------------


async def test_list_profiles_shape(client, maker):
    await seed_profiles_to_db(maker)
    r = await client.get(PROFILES)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == len(MediaType)
    types = {p["media_type"] for p in body}
    assert types == {mt.value for mt in MediaType}
    video = next(p for p in body if p["media_type"] == "video")
    assert video["version"] == PROFILE_VERSION
    assert video["fields"]["video_codec"]["type"] == "string"
    assert video["fields"]["video_codec"]["facetable"] is True


async def test_get_single_profile_and_404(client, maker):
    await seed_profiles_to_db(maker)
    r = await client.get(f"{PROFILES}/image")
    assert r.status_code == 200, r.text
    assert r.json()["media_type"] == "image"
    assert "width" in r.json()["fields"]
    assert (await client.get(f"{PROFILES}/nonsense")).status_code == 404


# --- P4-T5 CHECK constraint -------------------------------------------------


async def test_check_rejects_non_object_user_metadata(maker):
    async with maker() as s:
        lib = (
            await s.execute(
                text(
                    "INSERT INTO libraries (name, root_path) VALUES ('cklib', '/d') "
                    "RETURNING id"
                )
            )
        ).scalar()
        await s.commit()
    with pytest.raises(IntegrityError):
        async with maker() as s:
            await s.execute(
                text(
                    "INSERT INTO items (library_id, media_type, path, rel_path, "
                    "filename, size, mtime, user_metadata) VALUES "
                    "(:lib, 'other', '/d/a', 'a', 'a', 1, now(), '[]'::jsonb)"
                ),
                {"lib": lib},
            )
            await s.commit()


# --- P4-T2 extract validation wiring ---------------------------------------


@pytest.fixture
async def extract_db(maker, monkeypatch):
    """Bind the extract + index_sync modules to the migrated pgserver DB."""
    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    monkeypatch.setattr(extract_mod, "SessionLocal", maker)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)
    return maker


async def _make_audio_item(maker, path: str) -> str:
    from filearr.models import Item, Library

    async with maker() as s:
        lib = Library(name=f"lib-{path}", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.audio,
            path=path,
            rel_path="a.mp3",
            filename="a.mp3",
            extension="mp3",
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_invalid_field_dropped_valid_kept_job_green(extract_db, tmp_path, monkeypatch):
    """A wrong-typed declared field (year -> non-numeric dict) is dropped and
    recorded in _validation_errors; the valid + unregistered keys survive; the
    extract job never raises."""
    import filearr.tasks.extract as extract_mod
    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    f = tmp_path / "a.mp3"
    f.write_bytes(b"\x00")
    item_id = await _make_audio_item(extract_db, str(f))

    # A malformed extractor: 'year' declared int but handed a dict (coercion
    # can't tame this — validation must); 'artist' valid; 'weird_key' unregistered.
    def _bad_extractor(_path):
        return {"year": {"nope": 1}, "artist": "Real Artist", "weird_key": "keep me"}

    monkeypatch.setitem(extract_mod.EXTRACTORS, MediaType.audio, _bad_extractor)

    await extract_item(item_id)  # must NOT raise

    async with extract_db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    meta = item.metadata_
    assert "year" not in meta  # invalid field dropped, never merged
    assert meta["artist"] == "Real Artist"  # valid field kept
    assert meta["weird_key"] == "keep me"  # unregistered passthrough
    assert item.year is None  # typed column never got the invalid value
    ve = meta["_validation_errors"]
    assert isinstance(ve, list) and len(ve) == 1
    assert ve[0]["field"] == "year" and ve[0]["reason"]


async def test_valid_extractor_output_records_no_validation_errors(
    extract_db, tmp_path, monkeypatch
):
    import filearr.tasks.extract as extract_mod
    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    f = tmp_path / "b.mp3"
    f.write_bytes(b"\x00")
    item_id = await _make_audio_item(extract_db, str(f))

    def _good_extractor(_path):
        return {"artist": "A", "year": 1999, "some_future_key": "x"}

    monkeypatch.setitem(extract_mod.EXTRACTORS, MediaType.audio, _good_extractor)
    await extract_item(item_id)

    async with extract_db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert "_validation_errors" not in item.metadata_
    assert item.metadata_["year"] == 1999
    assert item.year == 1999
