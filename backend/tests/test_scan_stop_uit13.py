"""UI-T13 graceful-stop tests.

A graceful stop (POST /scans/{id}/stop) is DISTINCT from a hard cancel: it lets
the current batch finish, keeps everything already scanned, runs the post-walk
wrap-up RESTRICTED to seen items (move detection + tombstoning SKIPPED; sidecar
association + reindex still run), marks the run terminal `stopped`, and the job
SUCCEEDS (never `failed`). The next ordinary scan reconciles whatever the partial
walk didn't reach.

Coverage:
  * stop mid-walk keeps progress, defers extraction, marks `stopped`, no raise
  * partial stop does NOT tombstone unvisited-but-present items (invariant 4)
  * sidecar association runs on partial data and PRESERVES links (never destroys)
  * scoped-scan stop works + does not tombstone in-scope unvisited items
  * /stop endpoint 404 / 409 / idempotency matrix; cancel-vs-stop interaction
  * SSE emits a terminal `done` for a `stopped` run
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text, update
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


def _stub_extract_reindex(monkeypatch, scan_mod, deferred=None):
    async def _defer(item_ids, scan_run_id=None):
        if deferred is not None:
            deferred.extend(item_ids)

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)


def _flip_after_first_commit(monkeypatch, session, run_id, new_status):
    """Flip the run's status on an independent connection right after the first
    batch commit, so the scan's post-batch refresh observes it. Mirrors the
    cancel-abort test's technique."""
    maker2 = async_sessionmaker(session.bind, expire_on_commit=False)
    flipped = {"done": False}
    real_commit = session.commit

    async def spy_commit():
        await real_commit()
        if not flipped["done"]:
            async with maker2() as s2:
                await s2.execute(
                    update(ScanRun).where(ScanRun.id == run_id).values(status=new_status)
                )
                await s2.commit()
            flipped["done"] = True

    monkeypatch.setattr(session, "commit", spy_commit)


# --------------------------------------------------------------------------- #
# Graceful stop mid-walk keeps progress + defers extraction + marks stopped   #
# --------------------------------------------------------------------------- #
async def test_stop_mid_walk_keeps_progress(session, tmp_path, monkeypatch):
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(session, root, name="stop", enabled_categories=["image"])

    deferred: list[str] = []
    _stub_extract_reindex(monkeypatch, scan_mod, deferred)

    run = ScanRun(library_id=lib.id, stats={})
    session.add(run)
    await session.commit()
    run_id = run.id

    _flip_after_first_commit(monkeypatch, session, run_id, "stopping")

    # Must return normally (no raise) => the task SUCCEEDS, so its job is never
    # marked failed and its locks free like a completed scan.
    stats = await scan_mod._scan_body(session, lib, run)

    assert run.status == "stopped"
    assert stats.get("stopped") is True
    assert 0 < stats["seen"] < 600      # broke off mid-walk, kept what it saw
    assert stats["missing"] == 0        # nothing tombstoned
    assert stats["moved"] == 0
    assert "sidecars" in stats          # association wrap-up still ran

    # Everything seen is persisted active, and extraction was deferred for them.
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        persisted = (await s2.execute(select(ScanRun).where(ScanRun.id == run_id))).scalar_one()
        n_active = (await s2.execute(
            select(func.count()).select_from(Item).where(
                Item.library_id == lib.id, Item.status == ItemStatus.active)
        )).scalar_one()
    assert persisted.status == "stopped"
    assert n_active == stats["seen"]
    assert len(deferred) == stats["seen"]  # each seen image queued for extraction


# --------------------------------------------------------------------------- #
# A partial stop must NOT tombstone present-but-unvisited items (invariant 4)  #
# --------------------------------------------------------------------------- #
async def test_stop_does_not_tombstone_unvisited(session, tmp_path, monkeypatch):
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 400, "jpg")
    lib = await _mk_library(session, root, name="notomb", enabled_categories=["image"])
    _stub_extract_reindex(monkeypatch, scan_mod)

    # Full first scan -> 400 active items, all present on disk.
    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        assert (await s2.execute(
            select(func.count()).select_from(Item).where(Item.library_id == lib.id)
        )).scalar_one() == 400

    # Re-scan, stop after the first batch. The ~150 unvisited files still exist
    # on disk, so they MUST stay active — a partial walk cannot prove them gone.
    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    _flip_after_first_commit(monkeypatch, session, run2.id, "stopping")
    stats = await scan_mod._scan_body(session, lib, run2)

    assert run2.status == "stopped"
    assert stats["seen"] < 400
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        rows = (await s2.execute(text(
            "SELECT status, count(*) FROM items WHERE library_id = :lid GROUP BY status"
        ), {"lid": str(lib.id)})).all()
    counts = {r[0]: r[1] for r in rows}
    assert counts == {"active": 400}, f"partial stop tombstoned unvisited items: {counts}"


# --------------------------------------------------------------------------- #
# Sidecar association runs on partial data and PRESERVES links (never destroys)#
# --------------------------------------------------------------------------- #
async def test_stop_preserves_sidecar_links(session, tmp_path, monkeypatch):
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root, 300, "jpg")  # filler to force a batch boundary
    (root / "film.mkv").write_bytes(b"video-bytes")
    (root / "film.nfo").write_bytes(b"<movie><title>M</title></movie>")
    lib = await _mk_library(session, root, name="side",
                            enabled_categories=["image", "video"])
    _stub_extract_reindex(monkeypatch, scan_mod)

    # Full scan links film.nfo -> film.mkv.
    run1 = ScanRun(library_id=lib.id, stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1)
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        nfo = (await s2.execute(
            select(Item).where(Item.library_id == lib.id, Item.rel_path == "film.nfo")
        )).scalar_one()
        mkv = (await s2.execute(
            select(Item).where(Item.library_id == lib.id, Item.rel_path == "film.mkv")
        )).scalar_one()
        assert nfo.sidecar_of == mkv.id  # linked by the full scan

    # A stopped partial re-scan recomputes association over the whole active row
    # set; because tombstoning is skipped no parent leaves that set, so the link
    # is preserved (association only links, never destroys, for partial data).
    run2 = ScanRun(library_id=lib.id, stats={})
    session.add(run2)
    await session.commit()
    _flip_after_first_commit(monkeypatch, session, run2.id, "stopping")
    stats = await scan_mod._scan_body(session, lib, run2)

    assert run2.status == "stopped" and stats.get("stopped") is True
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        nfo = (await s2.execute(
            select(Item).where(Item.library_id == lib.id, Item.rel_path == "film.nfo")
        )).scalar_one()
        assert nfo.sidecar_of == mkv.id, "graceful stop destroyed an existing sidecar link"


# --------------------------------------------------------------------------- #
# Scoped-scan stop works + does not tombstone in-scope unvisited items         #
# --------------------------------------------------------------------------- #
async def test_scoped_scan_stop(session, tmp_path, monkeypatch):
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    _touch(root / "Downloads", 300, "jpg")
    lib = await _mk_library(session, root, name="scoped", enabled_categories=["image"])
    _stub_extract_reindex(monkeypatch, scan_mod)

    # Full scoped scan of Downloads -> 300 active items under the subtree.
    run1 = ScanRun(library_id=lib.id, rel_path="Downloads", stats={})
    session.add(run1)
    await session.commit()
    await scan_mod._scan_body(session, lib, run1, scope_rel="Downloads")

    # Stop the scoped re-scan after the first batch. In-scope unvisited items
    # must NOT be tombstoned.
    run2 = ScanRun(library_id=lib.id, rel_path="Downloads", stats={})
    session.add(run2)
    await session.commit()
    _flip_after_first_commit(monkeypatch, session, run2.id, "stopping")
    stats = await scan_mod._scan_body(session, lib, run2, scope_rel="Downloads")

    assert run2.status == "stopped"
    assert stats["scope"] == "Downloads"
    assert stats["seen"] < 300
    assert stats["missing"] == 0
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as s2:
        counts = {r[0]: r[1] for r in (await s2.execute(text(
            "SELECT status, count(*) FROM items WHERE library_id = :lid GROUP BY status"
        ), {"lid": str(lib.id)})).all()}
    assert counts == {"active": 300}, f"scoped stop tombstoned unvisited items: {counts}"


# --------------------------------------------------------------------------- #
# /stop endpoint: 404 / 409 / idempotency + cancel-vs-stop interaction         #
# --------------------------------------------------------------------------- #
async def test_stop_endpoint_matrix(engine, monkeypatch):
    import httpx

    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    async with maker() as s:
        lib = Library(name="e", root_path="/data")
        s.add(lib)
        await s.flush()
        running = ScanRun(library_id=lib.id, status="running", stats={},
                          started_at=datetime.now(UTC))
        finished = ScanRun(library_id=lib.id, status="finished", stats={},
                           started_at=datetime.now(UTC))
        cancelled = ScanRun(library_id=lib.id, status="cancelled", stats={},
                            started_at=datetime.now(UTC))
        already = ScanRun(library_id=lib.id, status="stopped", stats={},
                          started_at=datetime.now(UTC))
        s.add_all([running, finished, cancelled, already])
        await s.commit()
        rid, fid, cid, sid = running.id, finished.id, cancelled.id, already.id

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # running -> stopping (transient; finished_at stays null)
        r = await client.post(f"/api/v1/scans/{rid}/stop")
        assert r.status_code == 200 and r.json()["status"] == "stopping"
        # idempotent: stopping a scan already stopping is still 200
        r = await client.post(f"/api/v1/scans/{rid}/stop")
        assert r.status_code == 200 and r.json()["status"] == "stopping"
        # cancel now 409s (cancel requires 'running', and it is 'stopping')
        r = await client.post(f"/api/v1/scans/{rid}/cancel")
        assert r.status_code == 409
        # already stopped -> idempotent 200
        r = await client.post(f"/api/v1/scans/{sid}/stop")
        assert r.status_code == 200 and r.json()["status"] == "stopped"
        # finished / cancelled -> 409 conflict
        assert (await client.post(f"/api/v1/scans/{fid}/stop")).status_code == 409
        assert (await client.post(f"/api/v1/scans/{cid}/stop")).status_code == 409
        # unknown id -> 404
        assert (await client.post(f"/api/v1/scans/{uuid.uuid4()}/stop")).status_code == 404
    app.dependency_overrides.clear()

    async with maker() as s:
        row = (await s.execute(select(ScanRun).where(ScanRun.id == rid))).scalar_one()
    assert row.status == "stopping"
    assert row.finished_at is None  # transient marker: not finished yet


# --------------------------------------------------------------------------- #
# SSE emits a terminal `done` event for a `stopped` run                        #
# --------------------------------------------------------------------------- #
async def test_sse_terminal_stopped(engine, monkeypatch):
    from filearr.api import scans as scans_api

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scans_api, "SessionLocal", maker)

    async with maker() as s:
        lib = Library(name="sse", root_path="/data")
        s.add(lib)
        await s.flush()
        run = ScanRun(library_id=lib.id, status="stopped",
                      stats={"seen": 42, "stopped": True},
                      started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
        s.add(run)
        await s.commit()
        scan_id = run.id

    events = []
    async for ev in scans_api.scan_events(scan_id, _=None):
        events.append(ev)
        if ev.event == "done":
            break
    assert events, "no SSE events emitted"
    assert events[-1].event == "done"
    assert events[-1].data["status"] == "stopped"
    assert events[-1].data["stopped"] is True
