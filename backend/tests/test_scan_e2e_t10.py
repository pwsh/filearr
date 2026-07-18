"""T10 end-to-end scan + extract integration over the realistic `media_tree`
fixture (every enabled MediaType plus sidecars and junk).

Drives the real ``scan._scan_body`` against a migrated pgserver Postgres with
real files on disk, captures the extract jobs it batch-defers, runs the real
``extract_item`` task for each, and asserts:

  * one item per primary file, none for junk (dotfiles / *.partial);
  * ``stats.by_type`` (derived here from the items) is populated for EVERY
    enabled type;
  * every sidecar row is created, linked to a parent (``sidecar_of``) and marked
    hidden from default search (``is_sidecar``), not surfaced as a top-level hit;
  * the Kodi NFO title/plot folded into its parent's extracted metadata.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, ItemStatus, Library, ScanRun

from .conftest import psycopg3_uri

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The enabled types the tree covers (excludes `other`, which is unreachable here).
ENABLED_TYPES = [
    "video", "audio", "audiobook", "sample",
    "image", "model3d", "document", "spreadsheet",
]


@pytest.fixture
async def engine(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    eng = create_async_engine(psycopg3_uri(pg_uri))
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    yield eng
    await eng.dispose()


async def _run_scan_capturing_extracts(session, library) -> list[str]:
    """Run a real _scan_body but capture the ids it would batch-defer for extract
    (instead of enqueueing on Procrastinate) and no-op the Meili reindex."""
    from filearr.tasks import scan as scan_mod

    captured: list[str] = []

    async def _capture(item_ids, scan_run_id=None):
        captured.extend(item_ids)

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer = scan_mod._defer_extract_batch
    orig_reindex = scan_mod._reindex_library
    scan_mod._defer_extract_batch = _capture
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex
    return captured


async def test_e2e_scan_extract_all_types_and_sidecars(engine, media_tree, monkeypatch):
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # extract_item uses its own SessionLocal + defers an index sync; repoint the
    # session and no-op the index defer so no Procrastinate connection is needed.
    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    monkeypatch.setattr(extract_mod, "SessionLocal", Session)

    async def _noop_defer(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop_defer)

    async with Session() as session:
        lib = Library(
            name="everything",
            root_path=str(media_tree.root),
            enabled_types=ENABLED_TYPES,
            exclude_globs=["*.partial"],
        )
        session.add(lib)
        await session.commit()
        lib_id = lib.id

        extract_ids = await _run_scan_capturing_extracts(session, lib)

    # Run the real extractor for each deferred primary item.
    from filearr.tasks.extract import extract_item

    for iid in extract_ids:
        await extract_item(iid)

    async with Session() as session:
        items = (
            await session.execute(select(Item).where(Item.library_id == lib_id))
        ).scalars().all()

        by_type: dict[str, list[Item]] = {}
        for it in items:
            by_type.setdefault(it.media_type.value, []).append(it)

        primaries = [i for i in items if i.sidecar_of is None and not i.is_sidecar]
        sidecar_rows = [i for i in items if i.is_sidecar]

        # --- P6-T2: every scanned item is stamped with its ltree RBAC scope ---
        from filearr import rbac as _rbac

        for it in items:
            assert it.path_scope == _rbac.path_to_ltree(it.rel_path, library_id=lib_id), (
                f"{it.rel_path} path_scope not stamped/encoded correctly"
            )
            assert it.path_scope.startswith(_rbac.library_label(lib_id) + ".")

        # --- junk never became an item ---
        rels = {i.rel_path for i in items}
        for j in media_tree.junk:
            assert j not in rels, f"junk file leaked into catalog: {j}"

        # --- exactly the expected primary count ---
        assert len(primaries) == media_tree.primary_count

        # --- stats.by_type populated for EVERY enabled type ---
        stats_by_type = {
            t: len([i for i in primaries if i.media_type.value == t])
            for t in ENABLED_TYPES
        }
        for t in ENABLED_TYPES:
            assert stats_by_type[t] >= 1, f"no items catalogued for enabled type {t}"

        # --- every primary got extracted metadata (or a recorded error, never silence) ---
        for p in primaries:
            assert p.metadata_, f"{p.rel_path} has empty metadata after extract"

        # --- sidecars: created, linked to a parent, hidden from default search ---
        assert len(sidecar_rows) == len(media_tree.sidecars), (
            f"expected {len(media_tree.sidecars)} sidecars, got {len(sidecar_rows)}"
        )
        parent_ids = {p.id for p in primaries}
        for sc in sidecar_rows:
            assert sc.is_sidecar is True
            assert sc.sidecar_of in parent_ids, (
                f"sidecar {sc.rel_path} not linked to a primary parent"
            )

        # --- the NFO folded its title/plot into the video parent's metadata ---
        video = next(i for i in primaries if i.media_type.value == "video")
        # parent metadata should carry NFO-derived fields (title/plot)
        nfo_derived = (video.metadata_ or {})
        assert any(
            (nfo_derived.get(k) or (video.title if k == "nfo_title" else None))
            for k in ("nfo_title", "nfo_plot")
        ), f"NFO metadata not folded into parent: {nfo_derived}"

        # --- all rows are active (nothing wrongly tombstoned) ---
        assert all(i.status == ItemStatus.active for i in items)
