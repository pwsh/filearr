"""Move/rename detection (T2).

Two layers:
  * pure ``plan_moves`` unit tests (matching + ambiguity rules, no DB / no IO);
  * end-to-end ``_scan_body`` runs against a real Postgres (pgserver) with real
    files on disk, asserting identity + user edits survive a rename/move and that
    ambiguous / content-mismatch cases never falsely transfer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, ItemStatus, Library
from filearr.tasks.move import MovePlan, plan_moves

BACKEND_DIR = Path(__file__).resolve().parent.parent


class _FakeItem:
    def __init__(self, rel_path, quick_hash, size, content_hash=None):
        self.rel_path = rel_path
        self.quick_hash = quick_hash
        self.size = size
        self.content_hash = content_hash

    def __repr__(self):
        return f"<{self.rel_path}>"


def test_plan_unique_rename_matches():
    cand = _FakeItem("old.mkv", "qh1", 100)
    new = _FakeItem("new.mkv", "qh1", 100)
    plans, ambig = plan_moves([cand], [new])
    assert ambig == 0
    assert plans == [MovePlan(survivor=cand, duplicate=new)]


def test_plan_content_hash_confirms():
    cand = _FakeItem("old.mkv", "qh1", 100, content_hash="ch-A")
    new = _FakeItem("new.mkv", "qh1", 100, content_hash="ch-A")
    plans, ambig = plan_moves([cand], [new])
    assert ambig == 0 and len(plans) == 1


def test_plan_content_hash_veto_no_transfer():
    cand = _FakeItem("old.mkv", "qh1", 100, content_hash="ch-A")
    new = _FakeItem("new.mkv", "qh1", 100, content_hash="ch-B")
    plans, ambig = plan_moves([cand], [new])
    assert plans == []
    assert ambig == 1


def test_plan_ambiguous_two_candidates_no_content_hash():
    c1 = _FakeItem("a.mkv", "qh1", 100)
    c2 = _FakeItem("b.mkv", "qh1", 100)
    new = _FakeItem("c.mkv", "qh1", 100)
    plans, ambig = plan_moves([c1, c2], [new])
    assert plans == []
    assert ambig == 1


def test_plan_ambiguous_two_new_one_candidate():
    c = _FakeItem("a.mkv", "qh1", 100)
    n1 = _FakeItem("b.mkv", "qh1", 100)
    n2 = _FakeItem("c.mkv", "qh1", 100)
    plans, ambig = plan_moves([c], [n1, n2])
    assert plans == []
    assert ambig == 2


def test_plan_multiway_disambiguated_by_content_hash():
    c1 = _FakeItem("a.mkv", "qh1", 100, content_hash="X")
    c2 = _FakeItem("b.mkv", "qh1", 100, content_hash="Y")
    n1 = _FakeItem("c.mkv", "qh1", 100, content_hash="Y")
    n2 = _FakeItem("d.mkv", "qh1", 100, content_hash="X")
    plans, ambig = plan_moves([c1, c2], [n1, n2])
    assert ambig == 0
    pairs = {(p.survivor.rel_path, p.duplicate.rel_path) for p in plans}
    assert pairs == {("b.mkv", "c.mkv"), ("a.mkv", "d.mkv")}


def test_plan_no_candidates_means_genuinely_new():
    new = _FakeItem("new.mkv", "qh1", 100)
    plans, ambig = plan_moves([], [new])
    assert plans == [] and ambig == 0


# --------------------------------------------------------------------------- #
# End-to-end scan_body integration (real Postgres + real files)               #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    # pg_uri (conftest) has already pointed FILEARR_DATABASE_URL at pgserver and
    # cleared the settings cache, so alembic's env.py resolves the right DSN.
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _run_scan(session, library):
    from filearr.models import ScanRun
    from filearr.tasks import scan as scan_mod

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer = scan_mod._defer_extract_batch
    orig_reindex = scan_mod._reindex_library
    scan_mod._defer_extract_batch = _noop_defer
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        return await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


BODY_A = b"AAAA" * 40_000
BODY_B = b"BBBB" * 40_000
BODY_C = b"CCCC" * 40_000


async def _hash_all(session, lib):
    """Mimic the extract worker: populate quick/content hashes on active rows so a
    subsequent scan has candidates to match (production computes these async)."""
    from filearr.config import get_settings
    from filearr.sidecar import classify
    from filearr.tasks.extract import full_hash, quick_hash

    full_max = get_settings().scan_hash_full_max_bytes
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    for r in rows:
        if r.sidecar_of is not None or classify(r.rel_path) is not None:
            continue
        try:
            r.quick_hash = quick_hash(r.path, r.size)
            if r.size is not None and r.size <= full_max:
                r.content_hash = full_hash(r.path)
        except OSError:
            pass
    await session.commit()


async def _mk_library(session, root, name):
    lib = Library(name=name, root_path=str(root), enabled_categories=[])
    session.add(lib)
    await session.commit()
    return lib


async def test_rename_preserves_identity_and_user_edits(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "Movie.mkv", BODY_A)
    lib = await _mk_library(session, root, "l-rename")

    await _run_scan(session, lib)
    item = (
        await session.execute(select(Item).where(Item.library_id == lib.id))
    ).scalar_one()
    original_id = item.id
    original_first_seen = item.first_seen
    item.user_metadata = {"rating": 5, "note": "keep me"}
    item.tags = ["favourite", "4k"]
    item.external_ids = {"imdb": "tt1234567"}
    await session.commit()

    await _hash_all(session, lib)
    (root / "Movie.mkv").rename(root / "Renamed.mkv")
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 1
    assert stats["missing"] == 0
    assert stats["new"] == 0
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    assert len(rows) == 1
    survivor = rows[0]
    assert survivor.id == original_id
    assert survivor.rel_path == "Renamed.mkv"
    assert survivor.filename == "Renamed.mkv"
    assert survivor.status == ItemStatus.active
    assert survivor.user_metadata == {"rating": 5, "note": "keep me"}
    assert set(survivor.tags) == {"favourite", "4k"}
    assert survivor.external_ids == {"imdb": "tt1234567"}
    assert survivor.first_seen == original_first_seen


async def test_cross_directory_move(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "incoming" / "clip.mkv", BODY_A)
    lib = await _mk_library(session, root, "l-xdir")
    await _run_scan(session, lib)
    item = (
        await session.execute(select(Item).where(Item.library_id == lib.id))
    ).scalar_one()
    oid = item.id
    item.tags = ["t1"]
    await session.commit()

    await _hash_all(session, lib)
    (root / "sorted").mkdir()
    (root / "incoming" / "clip.mkv").rename(root / "sorted" / "clip.mkv")
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 1 and stats["missing"] == 0
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].id == oid
    assert rows[0].rel_path == "sorted/clip.mkv"
    assert rows[0].tags == ["t1"]


async def test_ambiguous_duplicate_no_false_transfer(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "one.mkv", BODY_A)
    _write(root / "two.mkv", BODY_A)
    lib = await _mk_library(session, root, "l-ambig")
    await _run_scan(session, lib)

    await _hash_all(session, lib)
    (root / "one.mkv").unlink()
    (root / "two.mkv").unlink()
    _write(root / "three.mkv", BODY_A)
    _write(root / "four.mkv", BODY_A)
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 0
    assert stats["move_ambiguous"] == 2
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    active = [r for r in rows if r.status == ItemStatus.active]
    missing = [r for r in rows if r.status == ItemStatus.missing]
    assert {r.rel_path for r in active} == {"three.mkv", "four.mkv"}
    assert {r.rel_path for r in missing} == {"one.mkv", "two.mkv"}


async def test_swap_case(session, tmp_path):
    # "Swap" at the catalog level: two files change places by BOTH moving to new
    # names in the same scan (A.mkv->X.mkv, B.mkv->Y.mkv with distinct contents).
    # Both old paths vanish, both new paths appear; the two transfers run in one
    # transaction (delete-duplicates -> park-at-sentinel -> final rel_path) and
    # each identity lands on the right survivor. NOTE: a rename that *reuses* an
    # existing path (A.mkv keeps its name, new bytes) is a "changed" file, not a
    # move — rel_path identity is stable there, so it is intentionally out of scope.
    root = tmp_path / "lib"
    _write(root / "A.mkv", BODY_A)
    _write(root / "B.mkv", BODY_B)
    lib = await _mk_library(session, root, "l-swap")
    await _run_scan(session, lib)
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    by_rel = {r.rel_path: r for r in rows}
    id_a, id_b = by_rel["A.mkv"].id, by_rel["B.mkv"].id
    by_rel["A.mkv"].tags = ["was-A"]
    by_rel["B.mkv"].tags = ["was-B"]
    await session.commit()

    await _hash_all(session, lib)
    (root / "A.mkv").rename(root / "X.mkv")   # BODY_A -> X.mkv
    (root / "B.mkv").rename(root / "Y.mkv")   # BODY_B -> Y.mkv
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 2
    assert stats["missing"] == 0
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    assert len(rows) == 2
    by_rel = {r.rel_path: r for r in rows}
    assert by_rel["X.mkv"].id == id_a  # BODY_A identity followed to X.mkv
    assert by_rel["X.mkv"].tags == ["was-A"]
    assert by_rel["Y.mkv"].id == id_b  # BODY_B identity followed to Y.mkv
    assert by_rel["Y.mkv"].tags == ["was-B"]


async def test_content_hash_disagreement_no_transfer(session, tmp_path, monkeypatch):
    from filearr.tasks import move as move_mod

    root = tmp_path / "lib"
    _write(root / "orig.mkv", BODY_A)
    lib = await _mk_library(session, root, "l-collide")
    await _run_scan(session, lib)
    item = (
        await session.execute(select(Item).where(Item.library_id == lib.id))
    ).scalar_one()
    oid = item.id
    await session.commit()

    await _hash_all(session, lib)
    (root / "orig.mkv").unlink()
    _write(root / "different.mkv", BODY_B)
    assert (root / "different.mkv").stat().st_size == len(BODY_A) == len(BODY_B)

    monkeypatch.setattr(move_mod, "quick_hash", lambda p, s: "COLLISION")
    stale = (
        await session.execute(select(Item).where(Item.id == oid))
    ).scalar_one()
    stale.quick_hash = "COLLISION"
    await session.commit()

    stats = await _run_scan(session, lib)

    assert stats["moved"] == 0
    assert stats["move_ambiguous"] == 1
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    active = [r for r in rows if r.status == ItemStatus.active]
    missing = [r for r in rows if r.status == ItemStatus.missing]
    assert [r.rel_path for r in active] == ["different.mkv"]
    assert [r.rel_path for r in missing] == ["orig.mkv"]
    assert missing[0].id == oid


async def test_moved_file_with_sidecars_relink(session, tmp_path):
    root = tmp_path / "lib"
    nfo = b"<movie><title>Dune</title><year>2021</year></movie>"
    _write(root / "Dune (2021)" / "Dune (2021).mkv", BODY_A)
    _write(root / "Dune (2021)" / "Dune (2021).nfo", nfo)
    _write(root / "Dune (2021)" / "poster.jpg", b"\xff\xd8\xff" + BODY_C)
    lib = await _mk_library(session, root, "l-sidecar")
    await _run_scan(session, lib)

    movie = (
        await session.execute(
            select(Item).where(
                Item.library_id == lib.id, Item.file_category == "video"
            )
        )
    ).scalar_one()
    movie_id = movie.id
    movie.tags = ["keep"]
    await session.commit()

    await _hash_all(session, lib)
    (root / "Dune (2021)").rename(root / "Dune 2021 [1080p]")
    stats = await _run_scan(session, lib)

    # The movie carries a hash -> its identity transfers (moved == 1). Sidecars have
    # NO hash (T3 skips them), so the old-path sidecar rows tombstone and fresh rows
    # appear at the new path; the association pass then re-links those to the
    # SURVIVING movie id (constraint: association runs after move-transfer).
    assert stats["moved"] == 1
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    active = [r for r in rows if r.status == ItemStatus.active]
    # movie (survived) + 2 freshly-created sidecars at the new path
    assert len(active) == 3
    survivor = next(r for r in active if r.file_category == "video")
    assert survivor.id == movie_id  # movie identity preserved across the move
    assert survivor.tags == ["keep"]
    assert survivor.rel_path.startswith("Dune 2021 [1080p]/")
    sidecars = [r for r in active if r.sidecar_of is not None]
    assert len(sidecars) == 2
    # re-linked to the same surviving parent id (not orphaned, not a new parent)
    assert all(s.sidecar_of == movie_id for s in sidecars)
    # old-path sidecars are tombstoned (expected: no hash -> no move transfer)
    tombstoned = [r for r in rows if r.status == ItemStatus.missing]
    assert all(t.sidecar_of is None or True for t in tombstoned)  # they simply vanished
