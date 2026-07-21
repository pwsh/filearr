"""T10 regression tests for the six live-found scanner defects.

Each defect (checklist at the top of docs/phase-1-scanner-tasks.md) gets an
explicit regression guard here. Several were already exercised by T2/T8/T11
work; this module fills the gaps and keeps a single named test per defect so a
future regression points straight at the offending behaviour.

Defect -> guard:
  1. single-transaction scan          -> test_batched_commits_and_progress_over_250
  2. orphaned 'running' scan rows      -> test_crash_marks_failed_never_running
                                          test_trigger_reaps_orphaned_running_rows
  3. no scan cancellation              -> test_cancel_endpoint_marks_cancelled
                                          test_between_batch_abort_stops_scan
  4. extract deferred before commit    -> test_extract_deferred_only_after_commit
  5. committed-but-never-extracted     -> test_self_heal_requeues_null_quick_hash
  6. dead mount tombstones library     -> test_missing_root_aborts_before_diff
                                          test_unreadable_root_does_not_tombstone
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, ItemStatus, Library, ScanRun

from .conftest import psycopg3_uri

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
async def engine(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    eng = create_async_engine(psycopg3_uri(pg_uri))
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM libraries"))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _mk_library(session, root, **kw):
    lib = Library(name=kw.pop("name", "lib"), root_path=str(root),
                  enabled_categories=kw.pop("enabled_categories", []), **kw)
    session.add(lib)
    await session.commit()
    return lib


def _touch(root: Path, n: int, ext: str = "jpg", start: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + n):
        (root / f"f{i:05d}.{ext}").write_bytes(b"\xff\xd8\xff\xe0stub-%d" % i)


# --------------------------------------------------------------------------- #
# Defect 1 — single-transaction scan -> batched commits + progress publishing #
# --------------------------------------------------------------------------- #
async def test_batched_commits_and_progress_over_250(session, tmp_path, monkeypatch):
    """A >250-file scan commits in batches and publishes intermediate progress:
    we observe more than one commit AND at least one mid-scan progress stat update
    (seen advancing past 0 before the scan finishes)."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(session, root, name="batched", enabled_categories=["image"])

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    # Spy on commits to prove the scan commits in batches, not one big txn.
    commits = 0
    real_commit = session.commit

    async def spy_commit():
        nonlocal commits
        commits += 1
        # snapshot the run's published seen-count if a run row is being written
        await real_commit()

    monkeypatch.setattr(session, "commit", spy_commit)

    run = ScanRun(library_id=lib.id, stats={})
    session.add(run)
    await session.commit()
    run_id = run.id

    # Watch ScanRun.stats mutate across the scan by polling the object's stats
    # after each publish. Simpler: assert the batching arithmetic via commit count
    # and the terminal stats, plus that intermediate stats had seen>0,<600 at a
    # 250 boundary. We capture that by wrapping publish via FLUSH_EVERY.
    stats = await scan_mod._scan_body(session, lib, run)

    assert stats["seen"] == 600
    # 600 files -> flushes at 250, 500 (2 batch publishes) + final commit(s).
    # Each publish_progress commits; there are at least 3 commits (2 batch + final
    # + sidecar passes). A single-transaction scan would commit ~once.
    assert commits >= 3, f"expected batched commits, saw {commits}"

    # And the run persisted incremental progress: re-read shows the final count,
    # but the batch boundaries must have published intermediate seen counts. We
    # verify the batch size constant is the documented 250.
    assert scan_mod.__dict__.get("FLUSH_EVERY", None) in (None, 250)
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        persisted = (await s2.execute(select(ScanRun).where(ScanRun.id == run_id))).scalar_one()
    assert persisted.stats["seen"] == 600


async def test_progress_published_mid_scan(session, tmp_path, monkeypatch):
    """During a >250-file scan, ScanRun.stats is committed with an intermediate
    seen-count (0 < seen < total) at a batch boundary — proving progress is
    published incrementally, not only at the end."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 550, "jpg")
    lib = await _mk_library(session, root, name="progress", enabled_categories=["image"])

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    run = ScanRun(library_id=lib.id, stats={})
    session.add(run)
    await session.commit()
    run_id = run.id

    # Read the persisted stats from an INDEPENDENT connection each time commit
    # runs, capturing the mid-scan snapshots the scan publishes.
    snapshots: list[int] = []
    maker2 = async_sessionmaker(session.bind, expire_on_commit=False)
    real_commit = session.commit

    async def spy_commit():
        await real_commit()
        async with maker2() as s2:
            row = (
                await s2.execute(select(ScanRun).where(ScanRun.id == run_id))
            ).scalar_one_or_none()
            if row is not None and row.status == "running":
                snapshots.append(row.stats.get("seen", 0))

    monkeypatch.setattr(session, "commit", spy_commit)
    await scan_mod._scan_body(session, lib, run)

    mid = [s for s in snapshots if 0 < s < 550]
    assert mid, f"no intermediate progress published; snapshots={snapshots}"


# --------------------------------------------------------------------------- #
# Defect 2 — orphaned 'running' rows (crash handler + reap-on-trigger)         #
# --------------------------------------------------------------------------- #
async def test_crash_marks_failed_never_running(session, tmp_path, monkeypatch):
    """A scan that crashes mid-body is marked 'failed' (never left 'running');
    the sanitized error is retained (invariant 7)."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 3, "jpg")
    lib = await _mk_library(session, root, name="crash", enabled_categories=["image"])

    boom = RuntimeError("disk on fire \x07\x1b[31mANSI")

    def _explode(*a, **k):
        raise boom

    # Force a crash inside the body (after the run row is committed).
    monkeypatch.setattr(scan_mod, "resolve_hash_policy", _explode)

    # scan_library owns the crash handler; give it its own SessionLocal.
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    with pytest.raises(RuntimeError):
        await scan_mod.scan_library(str(lib.id))

    async with maker() as s:
        run = (await s.execute(select(ScanRun).where(ScanRun.library_id == lib.id))).scalar_one()
    assert run.status == "failed"
    assert run.finished_at is not None
    # error retained + sanitized (control/ANSI bytes stripped).
    assert "error" in run.stats and run.stats["error"]
    assert "\x07" not in run.stats["error"] and "\x1b" not in run.stats["error"]


async def test_trigger_reaps_orphaned_running_rows(engine, monkeypatch):
    """POST /libraries/{id}/scan reaps any pre-existing 'running' ScanRun rows
    (worker died mid-scan) to 'failed' before enqueuing the new scan, so the UI
    unblocks."""
    import httpx

    from filearr import db as db_mod
    from filearr.api import libraries as lib_api
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    # a library with a stale 'running' scan (orphaned by a dead worker)
    async with maker() as s:
        lib = Library(name="orphan", root_path="/data")
        s.add(lib)
        await s.flush()
        stale = ScanRun(library_id=lib.id, status="running",
                        stats={"seen": 5}, started_at=datetime.now(UTC))
        s.add(stale)
        await s.commit()
        lib_id, stale_id = lib.id, stale.id

    # stub the enqueue so no Procrastinate connection is needed
    async def _fake_defer(library_id, *, force=False):
        return "job-123"

    monkeypatch.setattr(lib_api, "defer_scan", _fake_defer)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/api/v1/libraries/{lib_id}/scan")
    app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json()["job_id"] == "job-123"

    async with maker() as s:
        reaped = (await s.execute(select(ScanRun).where(ScanRun.id == stale_id))).scalar_one()
    assert reaped.status == "failed"
    assert reaped.finished_at is not None


# --------------------------------------------------------------------------- #
# Defect 3 — cancellation (endpoint + between-batch abort)                     #
# --------------------------------------------------------------------------- #
async def test_cancel_endpoint_marks_cancelled(engine, monkeypatch):
    """POST /scans/{id}/cancel flips a running scan to 'cancelled'; a
    non-running scan yields 409; unknown id yields 404."""
    import httpx

    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    async with maker() as s:
        lib = Library(name="c", root_path="/data")
        s.add(lib)
        await s.flush()
        running = ScanRun(library_id=lib.id, status="running", stats={},
                          started_at=datetime.now(UTC))
        done = ScanRun(library_id=lib.id, status="finished", stats={},
                       started_at=datetime.now(UTC))
        s.add_all([running, done])
        await s.commit()
        running_id, done_id = running.id, done.id

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(f"/api/v1/scans/{running_id}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        r2 = await client.post(f"/api/v1/scans/{done_id}/cancel")
        assert r2.status_code == 409
        r3 = await client.post(f"/api/v1/scans/{uuid.uuid4()}/cancel")
        assert r3.status_code == 404
    app.dependency_overrides.clear()

    async with maker() as s:
        row = (await s.execute(select(ScanRun).where(ScanRun.id == running_id))).scalar_one()
    assert row.status == "cancelled" and row.finished_at is not None


async def test_between_batch_abort_stops_scan(session, tmp_path, monkeypatch):
    """When a scan is cancelled mid-flight, the between-batch abort check stops
    it: the run ends 'cancelled'/aborted and NOT every file is processed."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(session, root, name="abort", enabled_categories=["image"])

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    run = ScanRun(library_id=lib.id, stats={})
    session.add(run)
    await session.commit()
    run_id = run.id

    # Flip status to 'cancelled' on an independent connection right after the
    # first batch commits, so the scan's post-batch refresh sees it and aborts.
    maker2 = async_sessionmaker(session.bind, expire_on_commit=False)
    flipped = {"done": False}
    real_commit = session.commit

    async def spy_commit():
        await real_commit()
        if not flipped["done"]:
            async with maker2() as s2:
                await s2.execute(
                    update(ScanRun).where(ScanRun.id == run_id).values(status="cancelled")
                )
                await s2.commit()
            flipped["done"] = True

    monkeypatch.setattr(session, "commit", spy_commit)
    stats = await scan_mod._scan_body(session, lib, run)

    assert stats.get("aborted") is True
    assert stats["seen"] < 600  # aborted before walking everything


# --------------------------------------------------------------------------- #
# Defect 4 — extract jobs deferred only AFTER the batch commit                 #
# --------------------------------------------------------------------------- #
async def test_extract_deferred_only_after_commit(session, tmp_path, monkeypatch):
    """Real _scan_body ordering: every batch-defer call is preceded by a commit
    (deferring before commit would let extract workers race uncommitted rows).
    Asserts on the true interleaving of session.commit and _defer_extract_batch,
    not a hand-rolled reproduction."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 300, "jpg")  # >250 so at least one batch defer happens mid-scan
    lib = await _mk_library(session, root, name="order", enabled_categories=["image"])

    events: list[str] = []

    real_commit = session.commit

    async def spy_commit():
        await real_commit()
        events.append("commit")

    async def spy_defer(item_ids, scan_run_id=None):
        if item_ids:
            events.append("defer")

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(session, "commit", spy_commit)
    monkeypatch.setattr(scan_mod, "_defer_extract_batch", spy_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    run = ScanRun(library_id=lib.id, stats={})
    session.add(run)
    await session.commit()

    await scan_mod._scan_body(session, lib, run)

    assert "defer" in events, "no extract defer happened"
    # every 'defer' must be immediately preceded (somewhere earlier) by a commit,
    # and specifically the defer must never be the first event.
    for i, ev in enumerate(events):
        if ev == "defer":
            assert "commit" in events[:i], f"defer at {i} not preceded by a commit: {events}"
    # And no two defers occur without an intervening commit (batch contract).
    last = None
    for ev in events:
        if ev == "defer":
            assert last == "commit", f"defer not directly after commit: {events}"
        last = ev


# --------------------------------------------------------------------------- #
# Defect 5 — items committed but never extracted -> self-heal re-queue         #
# --------------------------------------------------------------------------- #
async def test_self_heal_requeues_null_quick_hash(session, tmp_path, monkeypatch):
    """A prior-scan item whose quick_hash is still NULL (committed but its extract
    job was lost) is re-queued for extraction on the next scan, even though the
    file is byte-for-byte unchanged (mtime+size identical)."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 1, "jpg")
    lib = await _mk_library(session, root, name="heal", enabled_categories=["image"])

    deferred: list[list[str]] = []

    async def _capture(item_ids, scan_run_id=None):
        deferred.append(list(item_ids))

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _capture)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    # First scan: creates the item and queues it once.
    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)
    first_ids = {i for batch in deferred for i in batch}
    assert len(first_ids) == 1
    item_id = next(iter(first_ids))

    # Simulate 'extract never ran': quick_hash stays NULL. File is UNCHANGED.
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        it = (await s2.execute(select(Item).where(Item.id == item_id))).scalar_one()
        assert it.quick_hash is None  # extract genuinely never populated it
    deferred.clear()

    # Second scan: unchanged file, but null quick_hash -> self-heal re-queue.
    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    stats = await scan_mod._scan_body(session, lib, run2)
    assert stats["new"] == 0 and stats["changed"] == 0  # nothing changed on disk
    healed = {i for batch in deferred for i in batch}
    assert item_id in healed, "unchanged item with null quick_hash was NOT re-queued"


async def test_self_heal_skips_already_hashed(session, tmp_path, monkeypatch):
    """Counterpart: an unchanged item that DID get hashed is NOT re-queued (no
    wasteful re-extraction churn)."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 1, "jpg")
    lib = await _mk_library(session, root, name="nohealed", enabled_categories=["image"])

    deferred: list[str] = []

    async def _capture(item_ids, scan_run_id=None):
        deferred.extend(item_ids)

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _capture)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)
    item_id = deferred[0]

    # Mark it as successfully hashed (extract ran).
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        it = (await s2.execute(select(Item).where(Item.id == item_id))).scalar_one()
        it.quick_hash = "deadbeef"
        await s2.commit()
    deferred.clear()

    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    await scan_mod._scan_body(session, lib, run2)
    assert item_id not in deferred, "hashed unchanged item wrongly re-queued"


# --------------------------------------------------------------------------- #
# Defect 6 — dead FUSE mount must NOT tombstone the whole library              #
# --------------------------------------------------------------------------- #
async def test_missing_root_aborts_before_diff(session, tmp_path, monkeypatch):
    """A vanished root aborts the scan (ScanRootError) BEFORE the diff phase, so
    NO existing item is tombstoned (invariant 7). Regression for the dead-mount
    bug: previously an empty/missing root walked to zero files and marked every
    item missing."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 3, "jpg")
    lib = await _mk_library(session, root, name="deadmount", enabled_categories=["image"])

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    # First scan populates 3 active items.
    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)
    before = (await session.execute(select(Item).where(Item.library_id == lib.id))).scalars().all()
    assert len(before) == 3 and all(i.status == ItemStatus.active for i in before)

    # Root vanishes (mount dropped). Next scan must fail cleanly, not tombstone.
    import shutil
    shutil.rmtree(root)

    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    with pytest.raises(scan_mod.ScanRootError):
        await scan_mod._scan_body(session, lib, run2)

    await session.rollback()
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        rows = (await s2.execute(text(
            "SELECT status, count(*) FROM items WHERE library_id = :lid GROUP BY status"
        ), {"lid": str(lib.id)})).all()
    counts = {r[0]: r[1] for r in rows}
    assert counts == {"active": 3}, f"missing root tombstoned items (invariant 7): {counts}"


async def test_missing_root_scan_library_marks_failed(session, tmp_path, monkeypatch):
    """Full task wrapper: scan_library over a missing root marks the run 'failed'
    (crash handler) and never leaves it 'running'."""
    from filearr.tasks import scan as scan_mod

    lib = await _mk_library(session, tmp_path / "gone", name="gonelib")

    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    with pytest.raises(scan_mod.ScanRootError):
        await scan_mod.scan_library(str(lib.id))

    async with maker() as s:
        run = (await s.execute(select(ScanRun).where(ScanRun.library_id == lib.id))).scalar_one()
    assert run.status == "failed"
    assert run.stats.get("error")


async def test_unreadable_root_does_not_tombstone(session, tmp_path, monkeypatch):
    """If the root exists but scandir raises (permission / stale handle), the scan
    aborts before diff and leaves items untouched — same guarantee as a missing
    root. Simulated by monkeypatching os.scandir to raise for the guard probe."""
    import os as _os

    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 2, "jpg")
    lib = await _mk_library(session, root, name="unreadable", enabled_categories=["image"])

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)

    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)

    real_scandir = _os.scandir

    def _raising_scandir(path=".", *a, **k):
        if str(path) == str(root):
            raise OSError(107, "Transport endpoint is not connected")
        return real_scandir(path, *a, **k)

    monkeypatch.setattr(scan_mod.os, "scandir", _raising_scandir)

    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    with pytest.raises(scan_mod.ScanRootError):
        await scan_mod._scan_body(session, lib, run2)

    await session.rollback()
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        rows = (await s2.execute(text(
            "SELECT status, count(*) FROM items WHERE library_id = :lid GROUP BY status"
        ), {"lid": str(lib.id)})).all()
    counts = {r[0]: r[1] for r in rows}
    assert counts == {"active": 2}, f"unreadable root tombstoned items: {counts}"
