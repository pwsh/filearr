"""T7 — per-library hash policy.

Layers:
  * pure resolver matrix (no DB, no IO): auto+network -> quick_only,
    auto+local -> full, explicit full/quick_only, per-library ceiling override vs
    global, unknown-policy fail-safe;
  * end-to-end scan integration (real Postgres + real files) asserting the scan
    honours the resolved policy — content_hash present under full, absent under
    quick_only — and that move detection under quick_only refuses an ambiguous
    (quick_hash, size) collision and counts it as move_ambiguous;
  * API validation (positive ceiling, enum policy) via the create/PATCH schemas;
  * migration round-trip for the two new columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.hashpolicy import resolve_hash_policy
from filearr.models import HashPolicy, Item, Library, ScanRun  # noqa: F401

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Minimal mountinfo fixtures: one CIFS (network) mount and one ext4 (local) mount,
# both containing the same test path prefix so is_network_path classifies them.
_NET_MOUNTINFO = (
    "36 35 0:32 / /data rw,relatime shared:1 - cifs //srv/share rw,vers=3.0\n"
)
_LOCAL_MOUNTINFO = (
    "36 35 8:1 / /data rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
)
GLOBAL = 1_073_741_824  # 1 GiB (matches the default global ceiling)


# --------------------------------------------------------------------------- #
# QH-T1/T3 — quick_hash partial-read fix + full_hash xxh3-128 (pure, tmp files) #
# --------------------------------------------------------------------------- #
def _write_bytes(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_quick_hash_bug_zone_case_a(tmp_path):
    """Brief §2 Case A: two 100000-byte files (64KiB < size <= 128KiB) with an
    identical first 64 KiB but a DIFFERENT tail must NOT collide after QH-T1 (they
    did pre-fix — the head-only read never saw the differing bytes)."""
    from filearr.tasks.extract import QUICK_CHUNK, quick_hash

    size = 100_000
    a = bytes((i * 31 + 7) & 0xFF for i in range(size))
    b = bytearray(a)
    for i in range(QUICK_CHUNK, size):  # differ only past the head window
        b[i] ^= 0xFF
    pa = _write_bytes(tmp_path, "a.bin", a)
    pb = _write_bytes(tmp_path, "b.bin", bytes(b))
    assert quick_hash(pa, size) != quick_hash(pb, size)


def test_quick_hash_bug_zone_case_d_boundary(tmp_path):
    """Brief §2 Case D: the exact ==131072 (128 KiB) boundary. `size > 131072`
    excluded it pre-fix, so it was in the bug's blast radius; now it is read in
    full and differing tail bytes are covered."""
    from filearr.tasks.extract import QUICK_CHUNK, quick_hash

    size = QUICK_CHUNK * 2  # 131072
    a = bytes((i * 31 + 7) & 0xFF for i in range(size))
    b = bytearray(a)
    b[-1] ^= 0xFF  # a single differing byte in the tail
    pa = _write_bytes(tmp_path, "d_a.bin", a)
    pb = _write_bytes(tmp_path, "d_b.bin", bytes(b))
    assert quick_hash(pa, size) != quick_hash(pb, size)


def test_quick_hash_case_e_sampled_regime_unchanged(tmp_path):
    """Brief §2 Case E: one byte past the guard (131073) is sampled head+tail as
    before; a differing tail byte is still covered (regression guard)."""
    from filearr.tasks.extract import QUICK_CHUNK, quick_hash

    size = QUICK_CHUNK * 2 + 1  # 131073
    a = bytes((i * 31 + 7) & 0xFF for i in range(size))
    b = bytearray(a)
    b[-1] ^= 0xFF
    pa = _write_bytes(tmp_path, "e_a.bin", a)
    pb = _write_bytes(tmp_path, "e_b.bin", bytes(b))
    assert quick_hash(pa, size) != quick_hash(pb, size)


def test_full_hash_is_xxh3_128_32_hex(tmp_path):
    """QH-T3: content_hash digests are now 32 lowercase hex chars (xxh3-128).
    The low 64 bits equal the xxh3-64 quick digest of the same whole content — a
    cross-check that the widening is a superset, not a different function."""
    from filearr.tasks.extract import full_hash, quick_hash

    data = bytes((i * 17 + 3) & 0xFF for i in range(40_000))  # <=64KiB
    p = _write_bytes(tmp_path, "f.bin", data)
    ch = full_hash(p, len(data))
    assert len(ch) == 32
    assert all(c in "0123456789abcdef" for c in ch)
    # quick_hash of a <=64KiB file is the whole-file xxh3-64 → equals ch's low half.
    assert ch[16:] == quick_hash(p, len(data))


# --------------------------------------------------------------------------- #
# pure resolver matrix
# --------------------------------------------------------------------------- #
def test_auto_network_resolves_quick_only():
    r = resolve_hash_policy(
        declared="auto", root_path="/data/media", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_NET_MOUNTINFO,
    )
    assert r.policy == "quick_only"
    assert r.compute_content is False
    assert r.network is True


def test_auto_local_resolves_full():
    r = resolve_hash_policy(
        declared="auto", root_path="/data/media", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    assert r.policy == "full"
    assert r.compute_content is True
    assert r.network is False


def test_explicit_full_ignores_network():
    # An explicit 'full' overrides the network heuristic (no probe performed).
    r = resolve_hash_policy(
        declared="full", root_path="/data/media", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_NET_MOUNTINFO,
    )
    assert r.policy == "full"
    assert r.compute_content is True
    assert r.network is None  # not probed for an explicit policy


def test_explicit_quick_only_ignores_local():
    r = resolve_hash_policy(
        declared="quick_only", root_path="/data/media", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    assert r.policy == "quick_only"
    assert r.compute_content is False
    assert r.network is None


def test_per_library_ceiling_override_wins():
    r = resolve_hash_policy(
        declared="full", root_path="/x", hash_full_max_bytes=5_000,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    assert r.full_max_bytes == 5_000


def test_null_ceiling_falls_back_to_global():
    r = resolve_hash_policy(
        declared="full", root_path="/x", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    assert r.full_max_bytes == GLOBAL


def test_nonpositive_ceiling_falls_back_to_global():
    # A bad (0/negative) override never silently disables all full hashing.
    r = resolve_hash_policy(
        declared="full", root_path="/x", hash_full_max_bytes=0,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    assert r.full_max_bytes == GLOBAL


def test_unknown_policy_fails_safe_to_auto():
    r = resolve_hash_policy(
        declared="banana", root_path="/data", hash_full_max_bytes=None,
        global_max_bytes=GLOBAL, mountinfo=_NET_MOUNTINFO,
    )
    assert r.declared == "auto"
    assert r.policy == "quick_only"  # auto + network


def test_as_stats_is_json_safe():
    r = resolve_hash_policy(
        declared="auto", root_path="/data", hash_full_max_bytes=42,
        global_max_bytes=GLOBAL, mountinfo=_LOCAL_MOUNTINFO,
    )
    stats = r.as_stats()
    assert stats == {
        "declared": "auto", "resolved": "full", "compute_content": True,
        "full_max_bytes": 42, "network": False,
    }


# --------------------------------------------------------------------------- #
# end-to-end scan integration (real Postgres + real files)
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _run_scan(session, library):
    """Drive _scan_body with extract-defer + reindex stubbed out (as the T2 tests
    do), so we exercise the in-scan hashing path (move detection) deterministically
    without a live worker."""
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
        stats = await scan_mod._scan_body(session, library, run)
        return stats, run
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


BODY = b"ZZZZ" * 40_000


async def _mk_library(session, root, name, **kw):
    lib = Library(name=name, root_path=str(root), enabled_categories=[], **kw)
    session.add(lib)
    await session.commit()
    return lib


async def test_full_policy_computes_content_hash_via_move_path(session, tmp_path):
    """A 'full' library: a new file relocated between scans is content-hashed on
    demand by move detection, so the surviving row carries a content_hash."""
    root = tmp_path / "full"
    _write(root / "a.mkv", BODY)
    lib = await _mk_library(session, root, "full-lib", hash_policy="full")
    await _run_scan(session, lib)

    # Populate the first row's hashes (mimic the extract worker) then rename.
    from filearr.tasks.extract import full_hash, quick_hash
    row = (await session.execute(select(Item))).scalar_one()
    row.quick_hash = quick_hash(row.path, row.size)
    row.content_hash = full_hash(row.path)
    await session.commit()

    (root / "a.mkv").rename(root / "b.mkv")
    stats, _ = await _run_scan(session, lib)
    assert stats["moved"] == 1
    survivor = (await session.execute(select(Item))).scalar_one()
    assert survivor.rel_path == "b.mkv"
    # 'full' policy hashed the new row's content during move detection.
    assert survivor.content_hash is not None


async def test_quick_only_policy_skips_content_hash(session, tmp_path):
    """A 'quick_only' library never computes content_hash during the in-scan move
    path: two ambiguous same-quick_hash files stay unresolved (move_ambiguous)
    because content_hash cannot disambiguate them, and no false transfer happens."""
    root = tmp_path / "q"
    # Two identical-content files -> identical quick_hash+size (ambiguous bucket).
    _write(root / "one.mkv", BODY)
    _write(root / "two.mkv", BODY)
    lib = await _mk_library(session, root, "quick-lib", hash_policy="quick_only")
    await _run_scan(session, lib)

    # Give the prior rows quick_hash ONLY (quick_only never stored content_hash).
    from filearr.tasks.extract import quick_hash
    for row in (await session.execute(select(Item))).scalars():
        row.quick_hash = quick_hash(row.path, row.size)
    await session.commit()

    # Move BOTH to new names: the diff sees 2 vanished + 2 new, identical
    # quick_hash+size. Without content_hash the bucket is ambiguous -> refused.
    (root / "one.mkv").rename(root / "three.mkv")
    (root / "two.mkv").rename(root / "four.mkv")
    stats, _ = await _run_scan(session, lib)
    assert stats["moved"] == 0
    assert stats["move_ambiguous"] == 2  # integrity: never transferred blind
    # New rows created under quick_only carry no content_hash.
    rows = (await session.execute(select(Item).where(Item.status == "active"))).scalars().all()
    active_new = [r for r in rows if r.rel_path in ("three.mkv", "four.mkv")]
    assert len(active_new) == 2
    assert all(r.content_hash is None for r in active_new)


async def test_scan_records_resolved_policy_in_stats(session, tmp_path):
    root = tmp_path / "s"
    _write(root / "x.mkv", BODY)
    lib = await _mk_library(session, root, "stats-lib", hash_policy="quick_only")
    stats, run = await _run_scan(session, lib)
    assert stats["hash_policy"]["resolved"] == "quick_only"
    assert stats["hash_policy"]["declared"] == "quick_only"
    assert stats["hash_policy"]["compute_content"] is False
    # Persisted on the ScanRun row too.
    await session.refresh(run)
    assert run.stats["hash_policy"]["resolved"] == "quick_only"


# --------------------------------------------------------------------------- #
# API schema validation
# --------------------------------------------------------------------------- #
def test_schema_rejects_nonpositive_ceiling():
    from pydantic import ValidationError

    from filearr.schemas import LibraryIn

    with pytest.raises(ValidationError):
        LibraryIn(name="x", root_path="/d", hash_full_max_bytes=0)
    with pytest.raises(ValidationError):
        LibraryIn(name="x", root_path="/d", hash_full_max_bytes=-5)


def test_schema_rejects_unknown_policy():
    from pydantic import ValidationError

    from filearr.schemas import LibraryIn

    with pytest.raises(ValidationError):
        LibraryIn(name="x", root_path="/d", hash_policy="banana")


def test_schema_defaults_to_auto():
    from filearr.schemas import LibraryIn

    lib = LibraryIn(name="x", root_path="/d")
    assert lib.hash_policy == HashPolicy.auto
    assert lib.hash_full_max_bytes is None


def test_schema_accepts_valid_policy_and_ceiling():
    from filearr.schemas import LibraryIn

    lib = LibraryIn(
        name="x", root_path="/d", hash_policy="full", hash_full_max_bytes=2_000_000_000
    )
    assert lib.hash_policy is HashPolicy.full
    assert lib.hash_full_max_bytes == 2_000_000_000


# --------------------------------------------------------------------------- #
# migration round-trip for the two new columns
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pg_uri")
def test_migration_adds_and_drops_hash_columns(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(_psycopg3(pg_uri))
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("libraries")}
        assert "hash_policy" in cols
        assert "hash_full_max_bytes" in cols

        # Existing rows default to 'auto'.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO libraries (name, root_path) VALUES "
                    "('m7lib', '/data')"
                )
            )
            pol = conn.execute(
                text("SELECT hash_policy FROM libraries WHERE name='m7lib'")
            ).scalar()
        assert pol == "auto"

        # Downgrade to the T3 sidecar rev removes both columns.
        command.downgrade(cfg, "a1c3f7e9b204")
        cols = {c["name"] for c in inspect(engine).get_columns("libraries")}
        assert "hash_policy" not in cols
        assert "hash_full_max_bytes" not in cols

        # Re-upgrade is repeatable.
        command.upgrade(cfg, "head")
        cols = {c["name"] for c in inspect(engine).get_columns("libraries")}
        assert "hash_policy" in cols
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# extract-worker IO profile: the per-file worker honours the resolved policy    #
# (this is the T7 accept criterion — a library setting changes scan IO).        #
# --------------------------------------------------------------------------- #
async def _run_extract(Session, item_id):
    """Run extract_item against a monkeyed SessionLocal + no-op index defer."""
    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync

    orig_session = extract_mod.SessionLocal
    orig_defer = index_sync.sync_items.defer_async
    extract_mod.SessionLocal = Session

    async def _noop_defer(**_kw):
        return None

    index_sync.sync_items.defer_async = _noop_defer
    try:
        await extract_mod.extract_item(item_id)
    finally:
        extract_mod.SessionLocal = orig_session
        index_sync.sync_items.defer_async = _noop_defer  # leave patched harmlessly
        index_sync.sync_items.defer_async = orig_defer


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _seed_item(Session, tmp_path, name, hash_policy):
    from datetime import datetime

    from filearr.models import ItemStatus  # noqa: F401

    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    f = root / "clip.mkv"
    f.write_bytes(BODY)
    async with Session() as s:
        lib = Library(
            name=f"lib-{name}", root_path=str(root), enabled_categories=[],
            hash_policy=hash_policy,
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category="video", file_group="video", path=str(f),
            rel_path="clip.mkv", filename="clip.mkv", extension="mkv",
            size=f.stat().st_size, mtime=datetime.now(),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_extract_full_policy_writes_content_hash(maker, tmp_path):
    item_id = await _seed_item(maker, tmp_path, "efull", "full")
    await _run_extract(maker, item_id)
    async with maker() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.quick_hash is not None       # quick_hash always
    assert item.content_hash is not None      # full -> content hashed


async def test_extract_quick_only_skips_content_hash(maker, tmp_path):
    item_id = await _seed_item(maker, tmp_path, "equick", "quick_only")
    await _run_extract(maker, item_id)
    async with maker() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.quick_hash is not None        # quick_hash still computed
    assert item.content_hash is None          # quick_only -> NO content hash


async def test_extract_quick_only_small_file_gets_content_hash(maker, tmp_path):
    """QH-T2: a file <= 128 KiB gets a real content_hash even under quick_only
    (and even below the ceiling) — small files always get exact identity."""
    from datetime import datetime

    root = tmp_path / "esmall"
    root.mkdir(parents=True, exist_ok=True)
    f = root / "small.bin"
    f.write_bytes(b"AB" * 50_000)  # 100 KiB, in the 64-128 KiB band
    async with maker() as s:
        lib = Library(
            name="lib-esmall", root_path=str(root), enabled_categories=[],
            hash_policy="quick_only",
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category="other", file_group="other", path=str(f),
            rel_path="small.bin", filename="small.bin", extension="bin",
            size=f.stat().st_size, mtime=datetime.now(),
        )
        s.add(item)
        await s.commit()
        item_id = str(item.id)
    await _run_extract(maker, item_id)
    async with maker() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.quick_hash is not None
    # quick_only would normally skip content_hash, but a <=128KiB file always gets it.
    assert item.content_hash is not None
    assert len(item.content_hash) == 32  # xxh3-128


async def test_extract_full_policy_respects_ceiling(maker, tmp_path):
    """A per-library ceiling below the file size makes even a 'full' policy skip
    the content hash — proving the byte ceiling gates the expensive read."""
    from datetime import datetime

    root = tmp_path / "eceil"
    root.mkdir(parents=True, exist_ok=True)
    f = root / "big.mkv"
    f.write_bytes(BODY)  # 160 KiB
    async with maker() as s:
        lib = Library(
            name="lib-eceil", root_path=str(root), enabled_categories=[],
            hash_policy="full", hash_full_max_bytes=1_000,  # 1 KB ceiling < file
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category="video", file_group="video", path=str(f),
            rel_path="big.mkv", filename="big.mkv", extension="mkv",
            size=f.stat().st_size, mtime=datetime.now(),
        )
        s.add(item)
        await s.commit()
        item_id = str(item.id)
    await _run_extract(maker, item_id)
    async with maker() as s:
        item = (await s.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.quick_hash is not None
    assert item.content_hash is None          # file exceeds the per-library ceiling
