"""P4-T7/T8/T9 — item provenance columns, attributed ItemVersion writes, and the
non-'user' ItemVersion retention purge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from filearr.config import Settings, get_settings
from filearr.models import Library
from filearr.provenance import policy_version


def _library(**over) -> Library:
    base = dict(
        name="L",
        root_path="/data/l",
        hash_policy="auto",
        hash_full_max_bytes=None,
        enabled_types=[],
        include_globs=[],
        exclude_globs=[],
        enabled_presets=[],
        enabled_extension_groups=[],
    )
    base.update(over)
    return Library(**base)


# --------------------------------------------------------------------------- #
# P4-T7 policy_version fingerprint (pure)                                      #
# --------------------------------------------------------------------------- #
def test_policy_version_is_prefixed_and_stable():
    s = Settings()
    v1 = policy_version(_library(), s)
    v2 = policy_version(_library(), s)
    # QH-T4: scheme bumped cfg1 -> cfg2 for the hashing fix.
    assert v1.startswith("cfg2:")
    assert v1 == v2  # identical config -> identical fingerprint


def test_policy_version_scheme_bump_folds_hash_impl_version():
    """QH-T4/§9.1: the hashing-implementation marker is part of the fingerprint
    payload, so a hashing-behavior change bumps policy_version even with unchanged
    config. Guards against a future edit that forgets to fold it in."""
    import filearr.provenance as prov

    s = Settings()
    base = policy_version(_library(), s)
    orig = prov.HASH_IMPL_VERSION
    try:
        prov.HASH_IMPL_VERSION = orig + 1  # simulate a future hashing change
        bumped = policy_version(_library(), s)
    finally:
        prov.HASH_IMPL_VERSION = orig
    assert bumped != base  # config identical, only the impl marker moved


def test_policy_version_changes_when_scan_config_changes():
    s = Settings()
    base = policy_version(_library(), s)
    assert policy_version(_library(hash_policy="full"), s) != base
    assert policy_version(_library(root_path="/other"), s) != base
    assert policy_version(_library(exclude_globs=["*.tmp"]), s) != base
    assert policy_version(_library(hash_full_max_bytes=123), s) != base


def test_policy_version_is_order_insensitive_for_list_config():
    s = Settings()
    a = policy_version(_library(enabled_types=["video", "audio"]), s)
    b = policy_version(_library(enabled_types=["audio", "video"]), s)
    assert a == b  # reordering an array config value is not a change


def test_policy_version_tracks_global_ceiling():
    a = policy_version(_library(), Settings(scan_hash_full_max_bytes=1))
    b = policy_version(_library(), Settings(scan_hash_full_max_bytes=2))
    assert a != b


# --------------------------------------------------------------------------- #
# DB-backed provenance / attributed writes / purge                            #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def db(pg, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr import db as db_mod
    from filearr.models import Base

    uri = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("TRUNCATE item_versions, items, libraries CASCADE"))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    monkeypatch.setattr(extract_mod, "SessionLocal", Session)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)
    yield Session
    await engine.dispose()


def _facts(path):
    from pathlib import Path

    p = Path(path)
    return p.name, p.suffix.lstrip("."), p.stat().st_size


async def _seed(Session, path, media_type="document"):
    from filearr.models import Item, MediaType

    name, ext, size = _facts(path)
    async with Session() as s:
        lib = Library(name=f"lib-{name}", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType(media_type),
            path=str(path),
            rel_path=name,
            filename=name,
            extension=ext,
            size=size,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(lib.id), str(item.id)


async def _fetch_item(Session, item_id):
    from sqlalchemy import select

    from filearr.models import Item

    async with Session() as s:
        return (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()


async def _versions(Session, item_id):
    from sqlalchemy import select

    from filearr.models import ItemVersion

    async with Session() as s:
        return (
            (await s.execute(select(ItemVersion).where(ItemVersion.item_id == item_id)))
            .scalars()
            .all()
        )


async def test_extract_populates_provenance_columns(db, sample_pdf):
    from filearr.tasks.extract import extract_item

    _lib, item_id = await _seed(db, sample_pdf)
    await extract_item(item_id)
    item = await _fetch_item(db, item_id)
    assert item.policy_version and item.policy_version.startswith("cfg2:")
    # agent columns stay NULL in v1 (local-only scanning).
    assert item.source_agent_id is None
    assert item.replication_seq is None


async def test_policy_version_changes_when_library_config_changes(db, sample_pdf):
    from sqlalchemy import update

    from filearr.models import Library as Lib
    from filearr.tasks.extract import extract_item

    lib_id, item_id = await _seed(db, sample_pdf)
    await extract_item(item_id)
    first = (await _fetch_item(db, item_id)).policy_version

    async with db() as s:
        await s.execute(update(Lib).where(Lib.id == lib_id).values(hash_policy="full"))
        await s.commit()
    await extract_item(item_id)
    assert (await _fetch_item(db, item_id)).policy_version != first


async def test_attributed_version_row_only_on_change(db, sample_pdf):

    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    _lib, item_id = await _seed(db, sample_pdf)

    # First extract: metadata_ goes empty -> populated == a change -> exactly one row.
    await extract_item(item_id)
    rows = await _versions(db, item_id)
    assert len(rows) == 1
    assert rows[0].source == "extract:document"
    assert rows[0].actor == "extract:document"
    assert rows[0].patch  # non-empty diff

    # Byte-identical rescan: no value changed -> zero new rows.
    await extract_item(item_id)
    assert len(await _versions(db, item_id)) == 1

    # Mutate a stored metadata_ value; the extractor reproduces the original ->
    # a real change -> exactly one additional attributed row.
    from sqlalchemy import select

    async with db() as s:
        item = (
            await s.execute(select(Item).where(Item.id == item_id))
        ).scalar_one()
        item.metadata_ = {**item.metadata_, "title": "MUTATED"}
        await s.commit()
    await extract_item(item_id)
    rows = await _versions(db, item_id)
    assert len(rows) == 2
    assert all(r.source == "extract:document" for r in rows)


async def test_default_source_is_user_for_plain_inserts(db):
    from filearr.models import Item, ItemVersion, MediaType

    async with db() as s:
        lib = Library(name="uv", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.other,
            path="/root/a",
            rel_path="a",
            filename="a",
            extension=None,
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.flush()
        # A plain audit insert (as the PATCH/batch path does) omits source ->
        # server default backfills 'user' (the mechanism that backfilled every
        # pre-existing row).
        s.add(ItemVersion(item_id=item.id, actor="ui", patch={"title": "x"}))
        await s.commit()
        vid = item.id
    rows = await _versions(db, str(vid))
    assert len(rows) == 1
    assert rows[0].source == "user"


async def test_purge_deletes_non_user_rows_and_exempts_user(db, monkeypatch):
    from filearr.models import Item, ItemVersion, MediaType
    from filearr.worker import purge_item_versions

    monkeypatch.setattr(get_settings(), "audit_retention_days", 30)
    old = datetime.now(UTC) - timedelta(days=40)
    recent = datetime.now(UTC) - timedelta(days=1)

    async with db() as s:
        lib = Library(name="pl", root_path="/root")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.other,
            path="/root/p",
            rel_path="p",
            filename="p",
            extension=None,
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.flush()
        def _ver(source, when):
            return ItemVersion(
                item_id=item.id, actor="a", patch={}, source=source, changed_at=when
            )

        s.add_all(
            [
                _ver("extract:other", old),
                _ver("scan", old),
                _ver("extract:other", recent),
                _ver("user", old),
            ]
        )
        await s.commit()
        item_id = str(item.id)

    deleted = await purge_item_versions(0)
    assert deleted == 2  # the two OLD non-'user' rows

    rows = await _versions(db, item_id)
    sources = sorted(r.source for r in rows)
    # recent extract row survives; the OLD 'user' row survives regardless of age.
    assert sources == ["extract:other", "user"]
    assert any(r.source == "user" for r in rows)


# --------------------------------------------------------------------------- #
# Follow-up: stale error sentinels are cleared on a clean re-extract           #
# --------------------------------------------------------------------------- #
async def test_clean_reextract_clears_stale_error_sentinels(db, sample_pdf):
    from sqlalchemy import select

    from filearr.errors import extract_error_count
    from filearr.models import Item
    from filearr.tasks.extract import extract_item

    lib_id, item_id = await _seed(db, sample_pdf)

    # Simulate a PRIOR failed/invalid extract having left both sentinels behind.
    async with db() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
        item.metadata_ = {
            "_extract_error": "old ffprobe boom",
            "_validation_errors": [{"field": "year", "reason": "bad"}],
        }
        await s.commit()

    # Pre-condition: the GIN-backed count sees the errored item.
    async with db() as s:
        assert await extract_error_count(s, lib_id) == 1

    # A clean re-extract (the pdf extractor succeeds) drops the stale sentinels.
    await extract_item(item_id)
    item = await _fetch_item(db, item_id)
    assert "_extract_error" not in item.metadata_
    assert "_validation_errors" not in item.metadata_

    # ...and the authoritative GIN count reflects the recovery.
    async with db() as s:
        assert await extract_error_count(s, lib_id) == 0
