"""P2-T3 — extension-group resolution + scan wiring (ruling R5, union semantics).

Two layers:
  * pure unit tests over ``presets.resolve_enabled_extensions`` (union of groups
    per MediaType, None when unrefined, defensive enabled_categories gating);
  * a DB-integration scan over a real on-disk document tree proving the walk's
    extension filter narrows an enabled type to the union of the enabled groups,
    while an empty group set reproduces today's all-extensions behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, Library, ScanRun
from filearr.presets import resolve_enabled_extensions

from .conftest import psycopg3_uri

BACKEND_DIR = Path(__file__).resolve().parent.parent


# --- Pure resolution (R5) --------------------------------------------------


def test_no_groups_means_no_refinement():
    assert resolve_enabled_extensions("document", [], []) is None


def test_single_group_narrows_type():
    assert resolve_enabled_extensions("document", [], ["office_docs"]) == {
        "doc", "docx", "odt", "rtf",
    }


def test_multiple_groups_union_same_type():
    # R5: office_docs + ebooks both target `document` -> UNION.
    assert resolve_enabled_extensions(
        "document", [], ["office_docs", "ebooks"]
    ) == {"doc", "docx", "odt", "rtf", "epub", "mobi", "azw3", "cbz", "cbr"}


def test_group_for_other_type_does_not_refine():
    # A group targeting `image` leaves `document` unrefined (None).
    assert resolve_enabled_extensions("document", [], ["raw_photos"]) is None
    assert resolve_enabled_extensions("image", [], ["raw_photos"]) == {
        "cr2", "cr3", "nef", "arw", "dng", "raf",
    }


def test_disabled_type_returns_none():
    # enabled_categories non-empty + type not in it -> gated off elsewhere, no refine.
    assert resolve_enabled_extensions(
        "document", ["video"], ["office_docs"]
    ) is None


def test_unknown_group_ignored_in_resolution():
    # Validation is the API's job; resolution stays total.
    assert resolve_enabled_extensions("document", [], ["nope"]) is None


# --- Integration: extension filter in the scan walk ------------------------


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


def _doc_tree(root: Path) -> None:
    for rel in ("a.pdf", "b.docx", "c.epub", "d.txt"):
        (root / rel).write_bytes(b"x")


async def _scan_rels(session, library) -> set[str]:
    """Run a real _scan_body with extract-defer/reindex stubbed; return the
    rel_paths of the items the scan persisted for this library."""
    from filearr.tasks import scan as scan_mod

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer, orig_reindex = scan_mod._defer_extract_batch, scan_mod._reindex_library
    scan_mod._defer_extract_batch = _noop_defer
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex

    rows = (
        await session.execute(select(Item.rel_path).where(Item.library_id == library.id))
    ).scalars()
    return set(rows)


@pytest.mark.parametrize(
    ("categories", "groups", "expected"),
    [
        # W8-B taxonomy gating (the successor to the P2-T3 extension-group scan
        # refinement): a.pdf->pdf, b.docx->document-office, c.epub->ebook,
        # d.txt->document-text (all under file_category 'document').
        (["document"], [], {"a.pdf", "b.docx", "c.epub", "d.txt"}),   # whole category
        ([], ["document-office"], {"b.docx"}),                        # one group
        ([], ["document-office", "ebook"], {"b.docx", "c.epub"}),     # group OR (R5)
    ],
)
async def test_taxonomy_group_filter_in_scan(engine, tmp_path, categories, groups, expected):
    _doc_tree(tmp_path)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        lib = Library(
            name=f"docs-{'-'.join(categories + groups) or 'all'}",
            root_path=str(tmp_path),
            enabled_categories=categories,
            enabled_groups=groups,
        )
        session.add(lib)
        await session.commit()
        rels = await _scan_rels(session, lib)
    assert rels == expected
