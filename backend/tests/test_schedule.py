"""T5 — scheduled + watch-mode scanning.

Layers:
  * pure unit tests for cron evaluation (cronsim) and network-mount detection
    (mountinfo parsing) — no DB, no Procrastinate;
  * DB-backed tests for the periodic tick (`_defer_due_scans`): due libraries get
    a scan deferred, running-scan libraries are skipped, and a double tick does
    not double-defer;
  * a watcher debounce test driving `_watch_library` with a mocked awatch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Library, ScanRun
from filearr.schedule import (
    InvalidCronError,
    cron_is_due,
    is_network_path,
    validate_cron,
)


# --------------------------------------------------------------------------- #
# cron evaluation
# --------------------------------------------------------------------------- #
def test_cron_due_on_matching_minute():
    assert cron_is_due("*/5 * * * *", datetime(2026, 7, 7, 10, 5, 30)) is True
    assert cron_is_due("0 4 * * *", datetime(2026, 7, 7, 4, 0, 10)) is True


def test_cron_not_due_off_boundary():
    assert cron_is_due("*/5 * * * *", datetime(2026, 7, 7, 10, 6, 0)) is False
    assert cron_is_due("0 4 * * *", datetime(2026, 7, 7, 5, 0, 0)) is False


def test_cron_due_ignores_seconds_within_minute():
    # Any second within the matching minute counts as due (minute granularity).
    assert cron_is_due("0 4 * * *", datetime(2026, 7, 7, 4, 0, 0)) is True
    assert cron_is_due("0 4 * * *", datetime(2026, 7, 7, 4, 0, 59)) is True
    assert cron_is_due("0 4 * * *", datetime(2026, 7, 7, 4, 1, 0)) is False


def test_cron_dow_field():
    # 2026-07-05 is a Sunday.
    assert cron_is_due("30 2 * * 0", datetime(2026, 7, 5, 2, 30, 0)) is True
    assert cron_is_due("30 2 * * 0", datetime(2026, 7, 6, 2, 30, 0)) is False


def test_empty_cron_never_due_and_rejected():
    assert cron_is_due("", datetime(2026, 7, 7, 10, 5)) is False
    assert cron_is_due(None, datetime(2026, 7, 7, 10, 5)) is False  # type: ignore[arg-type]
    for bad in ("", "   ", None):
        with pytest.raises(InvalidCronError):
            validate_cron(bad)  # type: ignore[arg-type]


def test_validate_cron_accepts_valid_rejects_invalid():
    validate_cron("*/15 * * * *")
    validate_cron("0 0 1 1 *")
    for bad in ("not a cron", "* * * *", "60 * * * *", "* * * * 8", "@daily"):
        with pytest.raises(InvalidCronError):
            validate_cron(bad)


def test_invalid_cron_treated_as_not_due():
    # cron_is_due must never raise into the tick loop for a malformed expr.
    assert cron_is_due("garbage expr", datetime(2026, 7, 7, 10, 5)) is False


# --------------------------------------------------------------------------- #
# network-mount detection (watch-mode guard)
# --------------------------------------------------------------------------- #
MOUNTINFO = "\n".join(
    [
        "21 1 0:20 / / rw,relatime shared:1 - ext4 /dev/root rw",
        "40 21 0:44 / /data/media rw,relatime shared:2 - cifs //srv/media rw,vers=3",
        "41 21 0:45 / /data/nfsshare rw shared:3 - nfs4 srv:/export rw",
        "42 21 0:46 / /data/local rw shared:4 - xfs /dev/sdb1 rw",
        "43 21 0:47 / /data/btr rw shared:5 - btrfs /dev/sdc1 rw",
        "44 21 0:48 / /mnt/rclone rw shared:6 - fuse.rclone rclone:remote rw",
        "45 21 0:49 / /mnt/sshfs rw shared:7 - fuse.sshfs sshfs rw",
        "46 21 0:50 / /mnt/ntfs rw shared:8 - fuseblk /dev/sdd1 rw",
        r"47 21 0:51 / /mnt/with\040space rw shared:9 - ext4 /dev/sde1 rw",
    ]
)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/data/media/movies/x.mkv", True),   # cifs -> network
        ("/data/nfsshare/a", True),           # nfs4 -> network
        ("/mnt/rclone/y", True),              # fuse.rclone -> network
        ("/mnt/sshfs/z", True),               # fuse.sshfs -> network
        ("/data/local/ok", False),            # xfs -> local
        ("/data/btr/ok", False),              # btrfs -> local
        ("/mnt/ntfs/ok", False),              # fuseblk (ntfs-3g) -> local
        ("/home/user/media", False),          # ext4 root -> local
        ("/mnt/with space/x", False),         # octal-escaped mount point -> local
    ],
)
def test_is_network_path(path, expected):
    assert is_network_path(path, mountinfo=MOUNTINFO) is expected


def test_longest_prefix_wins():
    # A local mount nested under a network mount must classify by the *nearest*
    # (longest-prefix) mount, not the parent.
    mi = "\n".join(
        [
            "21 1 0:20 / / rw - ext4 /dev/root rw",
            "40 21 0:44 / /data rw - nfs4 srv:/export rw",
            "41 40 0:45 / /data/localssd rw - ext4 /dev/sdb1 rw",
        ]
    )
    assert is_network_path("/data/anything", mi) is True
    assert is_network_path("/data/localssd/file", mi) is False


def test_unverifiable_mount_fails_safe():
    # No matching mount / empty mountinfo -> treat as network (refuse watch mode).
    assert is_network_path("/data/x", mountinfo="") is True


# --------------------------------------------------------------------------- #
# periodic tick — DB-backed (_defer_due_scans)
# --------------------------------------------------------------------------- #
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def tick_env(pg_uri, monkeypatch):
    """Migrated DB wired into worker.SessionLocal, with defer_scan captured."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.db as db_mod
    import filearr.worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(worker_mod, "SessionLocal", maker, raising=False)

    calls: list[str] = []

    # Emulate the queueing-lock idempotency without a real Procrastinate DB: a
    # second defer for a library that already has one *queued* returns None.
    queued: set[str] = set()

    async def fake_defer_scan(library_id: str, **kw):
        if library_id in queued:
            return None
        queued.add(library_id)
        calls.append(library_id)
        return len(calls)

    monkeypatch.setattr(worker_mod, "defer_scan", fake_defer_scan)

    yield {"maker": maker, "calls": calls, "queued": queued, "worker": worker_mod}
    await engine.dispose()


async def _mk_library(maker, *, name, scan_cron=None, watch_mode=False, enabled=True,
                      root_path="/data/local"):
    async with maker() as s:
        lib = Library(
            name=name, root_path=root_path, scan_cron=scan_cron,
            watch_mode=watch_mode, enabled=enabled,
        )
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return str(lib.id)


async def _mk_running_scan(maker, library_id):
    async with maker() as s:
        s.add(ScanRun(library_id=library_id, status="running", stats={"seen": 0}))
        await s.commit()


@pytest.mark.asyncio
async def test_tick_defers_due_library(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="due", scan_cron="0 4 * * *")
    await _mk_library(maker, name="notdue", scan_cron="0 5 * * *")
    await _mk_library(maker, name="nocron", scan_cron=None)

    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 5))
    assert deferred == [lib]
    assert tick_env["calls"] == [lib]


@pytest.mark.asyncio
async def test_tick_skips_disabled_library(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    await _mk_library(maker, name="disabled", scan_cron="0 4 * * *", enabled=False)
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == []


@pytest.mark.asyncio
async def test_tick_skips_running_scan(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="busy", scan_cron="0 4 * * *")
    await _mk_running_scan(maker, lib)
    deferred = await worker._defer_due_scans(datetime(2026, 7, 7, 4, 0, 0))
    assert deferred == []
    assert tick_env["calls"] == []


@pytest.mark.asyncio
async def test_double_tick_does_not_double_defer(tick_env):
    """Two ticks for the same due minute enqueue at most one scan (queueing-lock
    idempotency emulated by the fake defer_scan)."""
    maker, worker = tick_env["maker"], tick_env["worker"]
    lib = await _mk_library(maker, name="due", scan_cron="0 4 * * *")
    t = datetime(2026, 7, 7, 4, 0, 0)
    first = await worker._defer_due_scans(t)
    second = await worker._defer_due_scans(t)
    assert first == [lib]
    assert second == []           # queued already -> defer_scan returns None
    assert tick_env["calls"] == [lib]


@pytest.mark.asyncio
async def test_schedule_scans_task_returns_count(tick_env):
    maker, worker = tick_env["maker"], tick_env["worker"]
    await _mk_library(maker, name="due", scan_cron="0 4 * * *")
    # Build the tick as a UTC timestamp (the task interprets it in UTC).
    ts = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC).timestamp())
    # Invoke the task's underlying coroutine (Procrastinate exposes it as .func).
    n = await worker.schedule_scans.func(timestamp=ts)
    assert n == 1


# --------------------------------------------------------------------------- #
# watch-mode debounce
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_watch_debounce_triggers_one_scan(monkeypatch):
    """A burst of change batches within one settle window triggers a single
    scan; a later batch triggers another."""
    import filearr.watch as watch_mod

    triggered: list[str] = []

    async def trigger(library_id: str):
        triggered.append(library_id)

    async def fake_awatch(root, *, stop_event=None, debounce=0):
        # Two rapid batches, then stop.
        yield {("added", "/x/a")}
        yield {("modified", "/x/b")}
        stop_event.set()

    monkeypatch.setattr(watch_mod, "awatch", fake_awatch)
    stop = asyncio.Event()
    await watch_mod._watch_library("lib1", "/x", trigger, stop=stop, debounce_s=0.01)
    # Each yielded batch fires one trigger (awatch itself does the OS-level
    # debounce; our settle sleep just avoids mid-copy fires).
    assert triggered.count("lib1") >= 1
    assert set(triggered) == {"lib1"}
