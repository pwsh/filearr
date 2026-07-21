"""DB round-trip tests for the T6 extractors through the real extract_item task:
new metadata lands in Item.metadata (not user_metadata) and corrupt files record
``_extract_error`` without the job raising.
"""

from __future__ import annotations

import pytest

pytest.importorskip("trimesh")

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
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


def _file_facts(path: str):
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


async def _fetch(Session, item_id):
    from sqlalchemy import select

    from filearr.models import Item

    async with Session() as s:
        return (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()


async def test_model3d_item_populates_metadata(db, stl_cube):
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "model3d", str(stl_cube))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert item.metadata_["triangles"] == 12
    assert item.metadata_["file_format"] == "stl"
    assert item.user_metadata in (None, {})  # extractor never touches user overlay
    assert "_extract_error" not in item.metadata_


async def test_document_pdf_item_populates_metadata(db, sample_pdf):
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "document", str(sample_pdf))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert item.metadata_["pages"] == 2
    assert item.metadata_["title"] == "The Title"
    assert item.user_metadata in (None, {})


async def test_spreadsheet_item_populates_metadata(db, sample_xlsx):
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "spreadsheet", str(sample_xlsx))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert item.metadata_["sheet_count"] == 2
    assert item.metadata_["sheets"] == ["Alpha", "Beta"]


async def test_document_pdf_body_text_lands_in_metadata(db, sample_pdf_text):
    # P3-T5: the body-text pass populates metadata_.body_text (extracted fact,
    # invariant 2) alongside properties — never user_metadata.
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "document", str(sample_pdf_text))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert "Hello body text extraction world" in item.metadata_["body_text"]
    assert item.metadata_["body_text_truncated"] is False
    assert item.user_metadata in (None, {})
    assert "_extract_error" not in item.metadata_


async def test_txt_body_text_supported_drops_unsupported_marker(db, sample_txt):
    # A txt file has no property parser (would be "unsupported"), but the body
    # pass makes it supported — the marker is dropped once body text exists.
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "document", str(sample_txt))
    await extract_item(item_id)
    item = await _fetch(db, item_id)
    assert "aardvarks" in item.metadata_["body_text"]
    assert "unsupported" not in item.metadata_


async def test_zipbomb_docx_rejected_records_error(db, zipbomb_docx):
    # The crafted ratio-bomb is rejected by the guard and surfaces as a visible
    # _extract_error rather than crashing the job or being parsed.
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "document", str(zipbomb_docx))
    await extract_item(item_id)  # must not raise
    item = await _fetch(db, item_id)
    assert "decompression guard" in item.metadata_["_extract_error"]


async def test_corrupt_model3d_records_error_without_raising(db, corrupt_stl):
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "model3d", str(corrupt_stl))
    await extract_item(item_id)  # must not raise
    item = await _fetch(db, item_id)
    assert item.metadata_["_extract_error"]


async def test_corrupt_document_records_error_without_raising(db, corrupt_pdf):
    from filearr.tasks.extract import extract_item

    item_id = await _make_item(db, "document", str(corrupt_pdf))
    await extract_item(item_id)  # must not raise
    item = await _fetch(db, item_id)
    assert item.metadata_["_extract_error"]
