"""Queued follow-up: a CLEAN re-extract clears the stale ``_extract_error`` /
``_validation_errors`` sentinels a PRIOR failed/invalid extract left behind, so a
recovered file stops surfacing in the error report.

The manual ``/retry-extracts`` endpoint path is covered by test_extract_hotfix; this
module exercises the AUTOMATIC path inside ``extract_item`` itself, end-to-end
against a real DB, and proves the item leaves the error-count surface
(``errors.extract_error_count`` / ``failing_items``, which key on ``_extract_error``).

Cases:
  * failed extract -> ``_extract_error`` present + counted + listed
  * clean re-extract -> marker gone + count 0 + no longer listed
  * re-extract that FAILS AGAIN -> single refreshed marker (JSONB dict; never dup)
  * DECOUPLED fix: extraction SUCCEEDS but validation fails -> stale
    ``_extract_error`` still cleared (it left the surface) while a fresh
    ``_validation_errors`` is recorded
  * clean re-extract also clears a stale ``_validation_errors``
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)

    from filearr.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync
    from filearr.config import get_settings

    monkeypatch.setattr(extract_mod, "SessionLocal", Session)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)
    # Keep extract_item on the direct path: no staged reschedule gate, no thumbnail
    # ride-along defer (irrelevant to the metadata assertions here).
    settings = get_settings()
    monkeypatch.setattr(settings, "staged_pipeline", False)
    monkeypatch.setattr(settings, "thumbs_enabled", False)
    monkeypatch.setattr(settings, "semantic_enabled", False)
    monkeypatch.setattr(settings, "disk_pg_path", None)
    yield Session
    await engine.dispose()


async def _mk_item(Session, *, metadata=None):
    from filearr.models import Item, Library, MediaType

    async with Session() as s:
        lib = Library(name=f"lib-{datetime.now(UTC).timestamp()}", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            status="active",
            path="/root/movie.mkv",  # does not exist -> hashing OSError swallowed
            rel_path="movie.mkv",
            filename="movie.mkv",
            extension="mkv",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=metadata or {},
        )
        s.add(item)
        await s.commit()
        return str(lib.id), str(item.id)


def _set_extractor(monkeypatch, fn):
    import filearr.tasks.extract as extract_mod

    monkeypatch.setitem(extract_mod.EXTRACTORS, extract_mod.MediaType.video, fn)


async def _meta(Session, item_id):
    from filearr.models import Item

    async with Session() as s:
        it = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        return dict(it.metadata_)


async def _error_surface(Session, lib_id, item_id):
    """(count, listed?) from the authoritative error-report queries."""
    from filearr.errors import extract_error_count, failing_items

    async with Session() as s:
        count = await extract_error_count(s, lib_id)
        listed = {r["id"] for r in await failing_items(s, lib_id)}
    return count, item_id in listed


# --------------------------------------------------------------------------- #
async def test_failed_then_clean_reextract_clears_and_leaves_surface(db, monkeypatch):
    from filearr.tasks.extract import extract_item

    lib_id, item_id = await _mk_item(db)

    # 1) extraction fails -> _extract_error present, counted + listed
    def _boom(_p):
        raise RuntimeError("ffprobe blew up")

    _set_extractor(monkeypatch, _boom)
    await extract_item(item_id)

    meta = await _meta(db, item_id)
    assert "ffprobe blew up" in meta["_extract_error"]
    count, listed = await _error_surface(db, lib_id, item_id)
    assert count == 1 and listed

    # 2) clean re-extract -> marker gone, count 0, no longer listed
    def _ok(_p):
        return {"title": "Movie", "year": 2007}

    _set_extractor(monkeypatch, _ok)
    await extract_item(item_id)

    meta = await _meta(db, item_id)
    assert "_extract_error" not in meta
    assert meta["title"] == "Movie" and meta["year"] == 2007
    count, listed = await _error_surface(db, lib_id, item_id)
    assert count == 0 and not listed


async def test_reextract_failing_again_refreshes_single_marker(db, monkeypatch):
    from filearr.tasks.extract import extract_item

    lib_id, item_id = await _mk_item(db, metadata={"_extract_error": "OLD message"})

    def _boom(_p):
        raise RuntimeError("NEW message")

    _set_extractor(monkeypatch, _boom)
    await extract_item(item_id)

    meta = await _meta(db, item_id)
    # JSONB is a dict: exactly one _extract_error key, carrying the refreshed text.
    assert "NEW message" in meta["_extract_error"]
    assert "OLD message" not in meta["_extract_error"]
    count, listed = await _error_surface(db, lib_id, item_id)
    assert count == 1 and listed


async def test_extract_success_but_validation_fails_still_leaves_error_surface(
    db, monkeypatch
):
    """DECOUPLED fix: extraction succeeding drops the stale ``_extract_error`` even
    when THIS run then fails profile validation — so a file that now parses leaves
    the (``_extract_error``-keyed) error surface, while the fresh
    ``_validation_errors`` marker still records the field problem."""
    import filearr.tasks.extract as extract_mod
    from filearr.tasks.extract import extract_item

    lib_id, item_id = await _mk_item(db, metadata={"_extract_error": "old parse fail"})

    def _ok(_p):
        return {"title": "Movie", "codec": "bogus"}

    _set_extractor(monkeypatch, _ok)
    # Force a validation violation on the (successfully extracted) codec field.
    monkeypatch.setattr(
        extract_mod,
        "validate_metadata",
        lambda _mt, _meta: [SimpleNamespace(field="codec", msg="not a valid codec")],
    )
    await extract_item(item_id)

    meta = await _meta(db, item_id)
    assert "_extract_error" not in meta  # stale parse error dropped
    assert meta["_validation_errors"][0]["field"] == "codec"
    assert "codec" not in meta  # the invalid field itself was stripped
    count, listed = await _error_surface(db, lib_id, item_id)
    assert count == 0 and not listed  # gone from the _extract_error surface


async def test_clean_reextract_clears_stale_validation_errors(db, monkeypatch):
    from filearr.tasks.extract import extract_item

    _lib, item_id = await _mk_item(
        db,
        metadata={
            "_validation_errors": [{"field": "x", "reason": "y"}],
            "title": "stale",
        },
    )

    def _ok(_p):
        return {"title": "Fresh", "year": 2020}

    _set_extractor(monkeypatch, _ok)
    await extract_item(item_id)

    meta = await _meta(db, item_id)
    assert "_validation_errors" not in meta  # stale validation marker cleared
    assert meta["title"] == "Fresh"  # extractor value merged over the stale one
    assert meta["year"] == 2020
