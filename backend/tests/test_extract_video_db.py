"""End-to-end test of the extract_item task for video items against a real
(ephemeral) Postgres, verifying merged ffprobe+guessit metadata lands in the
``metadata`` column and that a corrupt file records ``_extract_error`` without
the job raising.
"""

from __future__ import annotations

import pytest

from .conftest import requires_ffmpeg

pytestmark = [pytest.mark.asyncio, requires_ffmpeg]


@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
    """Async engine/session bound to pgserver, with schema created and the
    extract task's SessionLocal + index-sync defer monkeypatched in."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)

    from filearr.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    monkeypatch.setattr(extract_mod, "SessionLocal", Session)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)

    yield Session
    await engine.dispose()


def _file_facts(path: str) -> tuple[str, str, int]:
    from pathlib import Path

    p = Path(path)
    return p.name, p.suffix.lstrip("."), p.stat().st_size


async def _make_item(Session, media_type, path: str):
    from datetime import UTC, datetime

    from filearr.file_groups import detect_category, detect_group
    from filearr.models import Item, Library

    name, ext, size = _file_facts(path)
    async with Session() as s:
        lib = Library(name=f"lib-{name}", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category=detect_category(path),
            file_group=detect_group(path),
            path=path,
            rel_path=name,
            filename=name,
            extension=ext,
            size=size,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_extract_item_video_populates_tech_metadata(db, sample_mkv):
    from sqlalchemy import select

    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "video", str(sample_mkv))
    await extract_item(item_id)

    async with db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        meta = item.metadata_
        assert meta["video_codec"] == "h264"
        assert meta["resolution"] == "320x240"
        assert meta["duration"] == pytest.approx(1.0, abs=0.3)
        assert meta["audio_codec"] == "aac"
        assert meta["subtitle_tracks"][0]["codec"] == "subrip"
        assert "_extract_error" not in meta
        # hashes were still computed
        assert item.quick_hash and item.content_hash


async def test_extract_item_corrupt_sets_error_and_does_not_raise(db, corrupt_video):
    from sqlalchemy import select

    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "video", str(corrupt_video))
    # Must not raise despite the unreadable file.
    await extract_item(item_id)

    async with db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        assert "_extract_error" in item.metadata_
        assert item.metadata_["_extract_error"]
