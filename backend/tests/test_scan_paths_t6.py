"""P2-T6 — scan_paths table + hot-folder scheduling + scoped scans + watch.

Layers (mirroring test_schedule's discipline):
  * DB-backed scheduler tick (`_defer_due_scans`): a due scan_paths row defers a
    *scoped* scan; full-scan lock wins; same-subtree scoped scan blocks; a
    NULL-cron / disabled row schedules nothing; zero rows == T5 behaviour.
  * DB + real-files scoped scan execution (`_scan_body` scope_rel): writes/
    tombstones confined to the subtree, full-library existing map read-only for
    context (R3), out-of-scope vanished files NOT tombstoned, missing subtree
    finishes clean, in-scope move detection preserves identity.
  * WatchSupervisor._desired(): one watcher per watch-enabled path, per-path
    network re-check.
  * API CRUD + rel_path traversal / cron / watch-network validation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, ItemStatus, Library, ScanPath, ScanRun

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scan_paths"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))


# --------------------------------------------------------------------------- #
# scheduler tick — scoped scan_paths                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def tick_env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(worker_mod, "SessionLocal", maker, raising=False)

    calls: list[tuple[str, str | None]] = []
    queued: set[str] = set()  # keyed by the effective queueing lock

    async def fake_defer_scan(library_id, *, rel_path=None, queueing_lock=None):
        if queueing_lock is not None:
            lock = queueing_lock
        elif rel_path is not None:
            lock = f"scan:{library_id}:{rel_path}"
        else:
            lock = f"scan:{library_id}"
        if lock in queued:
            return None
        queued.add(lock)
        calls.append((library_id, rel_path))
        return len(calls)

    monkeypatch.setattr(worker_mod, "defer_scan", fake_defer_scan)
    yield {"maker": maker, "calls": calls, "worker": worker_mod}
    await engine.dispose()


async def _mk_library(maker, *, name, scan_cron=None, enabled=True, root_path="/data/local"):
    async with maker() as s:
        lib = Library(name=name, root_path=root_path, scan_cron=scan_cron, enabled=enabled)
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib.id


async def _mk_scan_path(maker, library_id, *, rel_path, scan_cron=None,
                        watch_mode=None, enabled=True):
    async with maker() as s:
        sp = ScanPath(
            library_id=library_id, rel_path=rel_path, scan_cron=scan_cron,
            watch_mode=watch_mode, enabled=enabled,
        )
        s.add(sp)
        await s.commit()
        await s.refresh(sp)
        return sp.id


async def _mk_running_scan(maker, library_id, *, rel_path=None):
    async with maker() as s:
        s.add(ScanRun(library_id=library_id, rel_path=rel_path, status="running", stats={}))
        await s.commit()


async def test_due_scan_path_defers_scoped_scan(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="nightly", scan_cron="0 4 * * *")
    await _mk_scan_path(maker, lib, rel_path="Downloads", scan_cron="* * * * *")
    # A minute where only the hot folder's cron is due (library nightly is not).
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 10, 30, 0))
    assert deferred == [f"{lib}:Downloads"]
    assert tick_env["calls"] == [(str(lib), "Downloads")]


async def test_full_and_scoped_both_due_defer_independently(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib", scan_cron="* * * * *")
    await _mk_scan_path(maker, lib, rel_path="Downloads", scan_cron="* * * * *")
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert str(lib) in deferred
    assert f"{lib}:Downloads" in deferred
    assert (str(lib), None) in tick_env["calls"]
    assert (str(lib), "Downloads") in tick_env["calls"]


async def test_scoped_skipped_while_full_scan_running(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib", scan_cron="0 4 * * *")
    await _mk_scan_path(maker, lib, rel_path="Downloads", scan_cron="* * * * *")
    await _mk_running_scan(maker, lib, rel_path=None)  # a FULL scan is running
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 10, 30, 0))
    assert deferred == []  # full-scan lock wins; scoped defer skipped
    assert tick_env["calls"] == []


async def test_full_skipped_while_scoped_scan_running(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib", scan_cron="* * * * *")
    await _mk_running_scan(maker, lib, rel_path="Downloads")  # a scoped scan running
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == []  # any running scan blocks the full schedule
    assert tick_env["calls"] == []


async def test_scoped_skipped_while_same_subtree_running(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib")
    await _mk_scan_path(maker, lib, rel_path="Downloads", scan_cron="* * * * *")
    await _mk_running_scan(maker, lib, rel_path="Downloads")
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == []


async def test_scoped_runs_while_other_subtree_running(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib")
    await _mk_scan_path(maker, lib, rel_path="Downloads", scan_cron="* * * * *")
    await _mk_running_scan(maker, lib, rel_path="Other")  # a DIFFERENT subtree
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == [f"{lib}:Downloads"]


async def test_null_cron_and_disabled_rows_schedule_nothing(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib", scan_cron=None)
    await _mk_scan_path(maker, lib, rel_path="Inherit", scan_cron=None)  # inherits
    await _mk_scan_path(maker, lib, rel_path="Off", scan_cron="* * * * *", enabled=False)
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == []


async def test_zero_scan_paths_matches_t5(tick_env):
    """Regression guard: a library with no scan_paths rows schedules exactly as
    T5 (library-level cron only)."""
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="lib", scan_cron="0 4 * * *")
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == [str(lib)]
    assert tick_env["calls"] == [(str(lib), None)]


# --------------------------------------------------------------------------- #
# scoped scan execution (R3)                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def scan_env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    from filearr.tasks import scan as scan_mod

    async def _noop_reindex(sess, lib_id):
        return None

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)
    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _noop_defer)
    yield {"maker": maker, "scan": scan_mod}
    await engine.dispose()


async def _run_scoped(scan_mod, session, library, scope_rel):
    run = ScanRun(library_id=library.id, rel_path=scope_rel, stats={})
    session.add(run)
    await session.commit()
    stats = await scan_mod._scan_body(session, library, run, scope_rel=scope_rel)
    return run, stats


async def test_scoped_scan_confines_writes_to_subtree(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    root = tmp_path
    (root / "Downloads").mkdir()
    (root / "Movies").mkdir()
    (root / "Downloads" / "a.mkv").write_bytes(b"a" * 10)
    (root / "Movies" / "b.mkv").write_bytes(b"b" * 10)

    async with maker() as s:
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        # Full scan first: both files indexed.
        await _run_scoped(scan_mod, s, lib, None)

    # Now DELETE the out-of-scope file and ADD a new in-scope file, then run a
    # scoped scan of Downloads only.
    (root / "Movies" / "b.mkv").unlink()
    (root / "Downloads" / "c.mkv").write_bytes(b"c" * 10)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        run, stats = await _run_scoped(scan_mod, s, lib, "Downloads")
        assert stats["scope"] == "Downloads"
        items = {i.rel_path: i for i in (
            await s.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()}
    # New in-scope file indexed.
    assert "Downloads/c.mkv" in items
    assert items["Downloads/c.mkv"].status == ItemStatus.active
    # Out-of-scope file that vanished is NOT tombstoned by a scoped scan.
    assert items["Movies/b.mkv"].status == ItemStatus.active
    # In-scope survivor still active.
    assert items["Downloads/a.mkv"].status == ItemStatus.active


async def test_scoped_scan_tombstones_in_scope_missing(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    root = tmp_path
    (root / "Downloads").mkdir()
    f1 = root / "Downloads" / "gone.mkv"
    f1.write_bytes(b"x" * 10)
    (root / "Downloads" / "stay.mkv").write_bytes(b"y" * 10)

    async with maker() as s:
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        await _run_scoped(scan_mod, s, lib, None)

    f1.unlink()  # an in-scope file genuinely removed

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run_scoped(scan_mod, s, lib, "Downloads")
        items = {i.rel_path: i for i in (
            await s.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()}
    assert items["Downloads/gone.mkv"].status == ItemStatus.missing
    assert items["Downloads/stay.mkv"].status == ItemStatus.active


async def test_scoped_scan_missing_subtree_no_tombstone(scan_env, tmp_path):
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    root = tmp_path
    (root / "Downloads").mkdir()
    (root / "Downloads" / "a.mkv").write_bytes(b"a" * 10)

    async with maker() as s:
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        await _run_scoped(scan_mod, s, lib, None)

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        # Scope a subtree that does not exist on disk (pre-created / vanished).
        run, stats = await _run_scoped(scan_mod, s, lib, "NeverExisted")
        assert stats.get("scope_missing") is True
        assert run.status == "finished"
        # Existing items untouched (no mass tombstone).
        a = (await s.execute(
            select(Item).where(Item.rel_path == "Downloads/a.mkv")
        )).scalars().one()
        assert a.status == ItemStatus.active


async def test_scoped_scan_preserves_identity_on_in_scope_move(scan_env, tmp_path):
    """A rename WITHIN the scope transfers identity onto the original row (uses
    the full-library existing map for move matching, R3)."""
    maker, scan_mod = scan_env["maker"], scan_env["scan"]
    root = tmp_path
    (root / "Downloads").mkdir()
    src = root / "Downloads" / "old.mkv"
    src.write_bytes(b"payload-bytes" * 100)

    async with maker() as s:
        lib = Library(name="L", root_path=str(root))
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        await _run_scoped(scan_mod, s, lib, None)
        # Seed the REAL quick_hash (extract didn't run in this test) + a user tag
        # so move detection can match the renamed file and we can prove identity
        # survival.
        from filearr.tasks.extract import quick_hash as _qh

        item = (await s.execute(
            select(Item).where(Item.rel_path == "Downloads/old.mkv")
        )).scalars().one()
        item.quick_hash = _qh(item.path, item.size)
        item.tags = ["keepme"]
        orig_id = item.id
        await s.commit()

    src.rename(root / "Downloads" / "new.mkv")

    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        await _run_scoped(scan_mod, s, lib, "Downloads")
        rows = {i.rel_path: i for i in (
            await s.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()}
    assert "Downloads/new.mkv" in rows
    assert "Downloads/old.mkv" not in rows
    moved = rows["Downloads/new.mkv"]
    assert moved.id == orig_id           # identity preserved
    assert moved.tags == ["keepme"]      # user edits carried over


# --------------------------------------------------------------------------- #
# watch supervisor — per-path watchers                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def watch_env(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield {"maker": maker}
    await engine.dispose()


async def test_desired_root_and_scoped_watchers(watch_env, monkeypatch):
    import filearr.watch as watch_mod

    maker = watch_env["maker"]

    # Per-path re-check: only a resolved subtree ending in "/net" is network;
    # the local root and the "Hot" subtree are local.
    def fake_net(path, mountinfo=None):
        return path.rstrip("/").endswith("/net")

    monkeypatch.setattr(watch_mod, "is_network_path", fake_net)

    async with maker() as s:
        lib = Library(name="L", root_path="/data/local", watch_mode=True)
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        s.add(ScanPath(library_id=lib.id, rel_path="Hot", watch_mode=True))
        # A subfolder resolving onto a network bind-mount: refused even though the
        # library root is local (defensive per-path re-check).
        s.add(ScanPath(library_id=lib.id, rel_path="net", watch_mode=True))
        # watch_mode NULL rows add no watcher.
        s.add(ScanPath(library_id=lib.id, rel_path="Cold", watch_mode=None))
        await s.commit()

    sup = watch_mod.WatchSupervisor(maker, trigger=None)
    desired = await sup._desired()
    # (abs_path, library_id, rel_path) tuples keyed by an opaque watcher key.
    by_rel = {v[2]: v for v in desired.values()}
    # Library root watcher (rel_path None) + Hot subtree watcher; net refused.
    assert None in by_rel and by_rel[None][0] == "/data/local"
    assert "Hot" in by_rel and by_rel["Hot"][0].endswith("/Hot")
    assert set(by_rel) == {None, "Hot"}


async def test_desired_refuses_network_root(watch_env, monkeypatch):
    import filearr.watch as watch_mod

    maker = watch_env["maker"]
    monkeypatch.setattr(watch_mod, "is_network_path", lambda p, mountinfo=None: True)
    async with maker() as s:
        lib = Library(name="L", root_path="/data/net", watch_mode=True)
        s.add(lib)
        await s.commit()
    sup = watch_mod.WatchSupervisor(maker, trigger=None)
    assert await sup._desired() == {}


# --------------------------------------------------------------------------- #
# API CRUD + validation                                                       #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    await _reset(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.maker = maker  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _make_lib(client, root_path="/data/local"):
    r = await client.post("/api/v1/libraries", json={"name": "L", "root_path": root_path})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_scan_path_crud_roundtrip(client, monkeypatch):
    import filearr.api.scan_paths as sp_api

    monkeypatch.setattr(sp_api, "is_network_path", lambda p: False)
    lib = await _make_lib(client)
    base = f"/api/v1/libraries/{lib}/scan-paths"

    r = await client.post(base, json={"rel_path": "Downloads", "scan_cron": "* * * * *"})
    assert r.status_code == 201, r.text
    sp = r.json()
    assert sp["rel_path"] == "Downloads"
    assert sp["scan_cron"] == "* * * * *"
    assert sp["watch_mode"] is None  # inherit

    listed = (await client.get(base)).json()
    assert len(listed) == 1

    r = await client.patch(f"{base}/{sp['id']}", json={"enabled": False, "scan_cron": None})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["scan_cron"] is None  # explicit null clears

    r = await client.delete(f"{base}/{sp['id']}")
    assert r.status_code == 204
    assert (await client.get(base)).json() == []


async def test_scan_path_normalizes_and_rejects_traversal(client, monkeypatch):
    import filearr.api.scan_paths as sp_api

    monkeypatch.setattr(sp_api, "is_network_path", lambda p: False)
    lib = await _make_lib(client)
    base = f"/api/v1/libraries/{lib}/scan-paths"

    # Traversal / absolute / drive inputs are rejected (422).
    for bad in ["../etc", "a/../../b", "/abs/path", "C:\\win", "a/./b"]:
        r = await client.post(base, json={"rel_path": bad})
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"

    # A trailing slash is trimmed; nested relative paths are kept.
    r = await client.post(base, json={"rel_path": "Downloads/Incoming/"})
    assert r.status_code == 201, r.text
    assert r.json()["rel_path"] == "Downloads/Incoming"
    # Empty rel_path ('' = whole library) is valid.
    r = await client.post(base, json={"rel_path": ""})
    assert r.status_code == 201
    assert r.json()["rel_path"] == ""


async def test_scan_path_rejects_bad_cron_and_duplicate(client, monkeypatch):
    import filearr.api.scan_paths as sp_api

    monkeypatch.setattr(sp_api, "is_network_path", lambda p: False)
    lib = await _make_lib(client)
    base = f"/api/v1/libraries/{lib}/scan-paths"

    r = await client.post(base, json={"rel_path": "X", "scan_cron": "not a cron"})
    assert r.status_code == 422

    assert (await client.post(base, json={"rel_path": "Dup"})).status_code == 201
    assert (await client.post(base, json={"rel_path": "Dup"})).status_code == 409


async def test_scan_path_watch_mode_network_refused(client, monkeypatch):
    import filearr.api.scan_paths as sp_api

    # Root is local, but the resolved subfolder path is network -> 422.
    monkeypatch.setattr(sp_api, "is_network_path", lambda p: "Net" in p)
    lib = await _make_lib(client)
    base = f"/api/v1/libraries/{lib}/scan-paths"

    r = await client.post(base, json={"rel_path": "Net", "watch_mode": True})
    assert r.status_code == 422
    # Non-watch row on the same path is fine (scan_cron works over network).
    r = await client.post(base, json={"rel_path": "Net", "scan_cron": "0 4 * * *"})
    assert r.status_code == 201


async def test_scan_paths_on_unknown_library_404(client):
    r = await client.get("/api/v1/libraries/00000000-0000-0000-0000-000000000000/scan-paths")
    assert r.status_code == 404
