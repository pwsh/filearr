"""UI-T14 — user-controllable job priorities + staged scan→extract pipeline.

Two user asks:
  1. jobs run at configurable priority (per-task-class defaults + a runtime
     "bump this queue's pending jobs" endpoint);
  2. scan and extract are STAGED so they don't hit disk/network at once — the
     scan finishes walking, THEN extraction is deferred (one chunked pass).

Coverage:
  * priority defaults are the config values and land on deferred jobs;
  * POST /system/jobs/priority: bounds (422), affected count, admin gate (401);
  * staged OFF preserves the T8 per-batch defer (regression);
  * staged ON: NO defers mid-walk + the FULL set deferred once at scan end;
  * gracefully-stopped scan defers the seen items; cancelled defers NOTHING
    (the committed strays keep quick_hash NULL -> next scan's self-heal);
  * extract reschedule gate: a scan walking the library -> extract reschedules
    (RescheduleExtract, never a failure); scan done -> extract proceeds.

Real Procrastinate schema + app tables on ONE throwaway pgserver Postgres
(mirrors test_throughput_t8 / test_jobs_dashboard_uit10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

procrastinate = pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg(module_db):
    return module_db


@pytest.fixture
async def env(pg):
    """Bind proc_app to the throwaway PG, apply the procrastinate + app schema,
    truncate between tests, yield a SQLAlchemy session maker. proc_app stays OPEN
    for the whole test so the in-worker defer helpers work directly."""
    import psycopg
    from procrastinate import PsycopgConnector
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.models import Base
    from filearr.worker import proc_app

    dsn = pg.get_uri()
    connector = PsycopgConnector(conninfo=dsn)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            engine = create_async_engine(
                dsn.replace("postgresql://", "postgresql+psycopg://", 1)
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
                conn.execute("TRUNCATE items, scan_runs, libraries CASCADE")
            maker = async_sessionmaker(engine, expire_on_commit=False)
            yield maker
            await engine.dispose()
    proc_app.connector = original


def _fetch_jobs(pg):
    import psycopg

    with psycopg.connect(pg.get_uri()) as conn:
        return conn.execute(
            "SELECT task_name, queue_name, priority, status::text, args "
            "FROM procrastinate_jobs ORDER BY id"
        ).fetchall()


def _touch(root: Path, n: int, ext: str = "jpg", start: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + n):
        (root / f"f{i:05d}.{ext}").write_bytes(b"\xff\xd8\xff\xe0stub-%d" % i)


async def _mk_library(maker, root, **kw):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(
            name=kw.pop("name", "lib"),
            root_path=str(root),
            enabled_categories=kw.pop("enabled_categories", []),
            **kw,
        )
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib


def _stub_defers(monkeypatch, scan_mod, calls):
    """Capture every _defer_extract_batch call (COPY ids — the caller may clear
    the source list) and no-op the reindex."""

    async def _defer(item_ids, scan_run_id=None):
        calls.append(list(item_ids))

    async def _noop_reindex(sess, lib_id):
        return None

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _defer)
    monkeypatch.setattr(scan_mod, "_reindex_library", _noop_reindex)


def _flip_after_first_commit(monkeypatch, session, run_id, new_status):
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from filearr.models import ScanRun

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
# Part 1 — priority defaults + endpoint                                       #
# --------------------------------------------------------------------------- #
async def test_task_defaults_are_the_config_values(env):
    """Every task class carries its UI-T14 default priority (higher = sooner)."""
    from filearr.config import get_settings
    from filearr.tasks.extract import extract_item
    from filearr.tasks.index_sync import rebuild_index, sync_items

    s = get_settings()
    assert extract_item.priority == s.extract_priority == -10
    assert sync_items.priority == s.index_priority == 0
    assert rebuild_index.priority == s.index_priority == 0


async def test_batch_defer_applies_extract_priority(env, pg):
    from filearr.config import get_settings
    from filearr.tasks.scan import _defer_extract_batch

    await _defer_extract_batch(["a", "b", "c"], "run-1")
    rows = _fetch_jobs(pg)
    assert len(rows) == 3
    s = get_settings()
    for task_name, queue, priority, status, _args in rows:
        assert task_name == "filearr.tasks.extract.extract_item"
        assert queue == s.queue_extract
        assert priority == s.extract_priority
        assert status == "todo"


async def test_batch_defer_chunks_large_input(monkeypatch):
    """A staged whole-library defer is chunked at DEFER_CHUNK ids per multi-row
    INSERT (never one oversized transaction). Deferrer faked -> no DB needed."""
    import filearr.tasks.scan as scan_mod

    monkeypatch.setattr(scan_mod, "DEFER_CHUNK", 100)
    sizes: list[int] = []

    class FakeDeferrer:
        async def batch_defer_async(self, *jobs):
            sizes.append(len(jobs))

    monkeypatch.setattr(
        scan_mod.proc_app, "configure_task", lambda *a, **k: FakeDeferrer()
    )
    await scan_mod._defer_extract_batch([f"i{i}" for i in range(250)], "run-x")
    assert sizes == [100, 100, 50]


async def _client(env, monkeypatch):
    import httpx

    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    monkeypatch.setattr(db_mod, "SessionLocal", env)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with env() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://t"), settings


async def test_priority_endpoint_updates_pending_count(env, pg, monkeypatch):
    import psycopg

    # Seed 4 todo extract jobs + 1 doing job on the extract queue.
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        for _ in range(4):
            conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES ('extract','filearr.tasks.extract.extract_item','{}'::jsonb,"
                "'todo'::procrastinate_job_status)"
            )
        conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
            "VALUES ('extract','filearr.tasks.extract.extract_item','{}'::jsonb,"
            "'doing'::procrastinate_job_status)"
        )

    app, client, _settings = await _client(env, monkeypatch)
    async with client:
        r = await client.post(
            "/api/v1/system/jobs/priority",
            json={"queue": "extract", "priority": 20, "scope": "pending"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["updated"] == 4  # only the 4 todo jobs, NOT the doing one
    app.dependency_overrides.clear()

    with psycopg.connect(pg.get_uri()) as conn:
        rows = conn.execute(
            "SELECT status::text, priority FROM procrastinate_jobs ORDER BY id"
        ).fetchall()
    for status, prio in rows:
        assert prio == (20 if status == "todo" else 0)


async def test_priority_endpoint_bounds_422(env, monkeypatch):
    app, client, _ = await _client(env, monkeypatch)
    async with client:
        for bad in (101, -101):
            r = await client.post(
                "/api/v1/system/jobs/priority",
                json={"queue": "extract", "priority": bad},
            )
            assert r.status_code == 422, (bad, r.text)
    app.dependency_overrides.clear()


async def test_priority_endpoint_admin_gate_401(env, monkeypatch):
    app, client, settings = await _client(env, monkeypatch)
    # Flip auth ON: the endpoint requires admin scope, so a token-less call 401s.
    monkeypatch.setattr(settings, "auth_enabled", True)
    async with client:
        r = await client.post(
            "/api/v1/system/jobs/priority",
            json={"queue": "extract", "priority": 5},
        )
        assert r.status_code == 401, r.text
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Part 2 — staged pipeline                                                     #
# --------------------------------------------------------------------------- #
async def test_staged_off_defers_per_batch(env, tmp_path, monkeypatch):
    """Regression: with staging OFF, extraction trickles out per committed batch
    (T8 behaviour) — multiple defer calls across the walk."""
    import filearr.tasks.scan as scan_mod
    from filearr.config import get_settings
    from filearr.models import ScanRun

    monkeypatch.setattr(get_settings(), "staged_pipeline", False)
    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(env, root, name="off", enabled_categories=["image"])

    calls: list[list[str]] = []
    _stub_defers(monkeypatch, scan_mod, calls)

    async with env() as session:
        run = ScanRun(library_id=lib.id, stats={})
        session.add(run)
        await session.commit()
        stats = await scan_mod._scan_body(session, lib, run)

    assert stats["seen"] == 600
    # Scan telemetry: total on-disk bytes walked, accumulated during the walk
    # (Item has no scan_run_id, so this cannot be re-derived afterwards). Compare
    # against the real files rather than a hardcoded number.
    expected_bytes = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
    assert stats["bytes_seen"] == expected_bytes > 0
    assert stats["bytes_per_s"] >= 0
    # 600 files @ FLUSH_EVERY=250 -> 2 mid-walk batches + 1 final = >=2 defer calls.
    assert len(calls) >= 2
    total = sum(len(c) for c in calls)
    assert total == 600


async def test_scan_counts_gate_excluded_files(env, tmp_path, monkeypatch):
    """The category/group gate reports HOW MANY files it rejected.

    Without this the only visible number is `seen`, so a library whose selection
    silently drops most of a folder looks like a broken scan ("the folder has 99k
    files but the library shows 77k") with nothing to explain it.
    """
    import filearr.tasks.scan as scan_mod
    from filearr.models import ScanRun

    root = tmp_path / "lib"
    _touch(root, 10, "jpg")          # image  -> admitted
    _touch(root, 4, "txt", start=100)  # document -> rejected by the gate
    # Only images are enabled, so the .txt files must be counted, not silent.
    lib = await _mk_library(env, root, name="gated", enabled_categories=["image"])
    _stub_defers(monkeypatch, scan_mod, [])

    async with env() as session:
        run = ScanRun(library_id=lib.id, stats={})
        session.add(run)
        await session.commit()
        stats = await scan_mod._scan_body(session, lib, run)

    assert stats["seen"] == 10
    assert stats["excluded_gate"] == 4
    assert stats["excluded"] == 4
    # Nothing here is spec-excluded, pruned or unreadable.
    assert stats["excluded_filtered"] == 0
    assert stats["pruned_dirs"] == 0
    assert stats["permission_denied"] == 0
    # The headline invariant the UI relies on: seen + excluded == enumerated.
    assert stats["seen"] + stats["excluded"] == 14


def _audit_tree(tmp_path):
    """keep.mkv + 2 spec-excluded files + a pruned dir holding 3 files."""
    (tmp_path / "keep.mkv").write_bytes(b"a")
    (tmp_path / "skip.tmp").write_bytes(b"b")
    (tmp_path / ".hidden").write_bytes(b"c")
    pruned = tmp_path / "node_modules"
    (pruned / "deep").mkdir(parents=True)
    (pruned / "buried.mkv").write_bytes(b"d")
    (pruned / "deep" / "a.js").write_bytes(b"e")
    (pruned / "deep" / "b.js").write_bytes(b"f")


def test_walk_audit_tallies_spec_exclusions_and_prunes(tmp_path):
    """`walk` fills a WalkAudit for its silent drop paths.

    Default (count_pruned off): files inside a pruned tree are counted NOWHERE,
    so the tally is explicitly a lower bound.
    """
    from pathspec import GitIgnoreSpec

    from filearr.tasks.scan import WalkAudit, walk

    _audit_tree(tmp_path)
    spec = GitIgnoreSpec.from_lines(["*.tmp", ".*", "node_modules/"])
    audit = WalkAudit()
    rels = {rel for _p, rel, _s, _m in walk(str(tmp_path), spec, audit=audit)}

    assert rels == {"keep.mkv"}
    assert audit.excluded_filtered == 2            # skip.tmp + .hidden
    assert audit.pruned_dirs == 1                  # node_modules
    assert audit.pruned_paths == ["node_modules"]  # named, not just counted
    # The whole point: 3 files are inside the pruned tree and are invisible.
    assert audit.pruned_files == 0
    assert audit.count_pruned is False


def test_walk_audit_counts_pruned_files_when_opted_in(tmp_path):
    """count_pruned makes the accounting reconcile EXACTLY.

    seen + excluded + pruned_files == files on disk (6 here: keep.mkv,
    skip.tmp, .hidden, buried.mkv, deep/a.js, deep/b.js). Without the opt-in
    that identity is a lower bound, which is what made a live 21,978-file gap
    unexplainable.
    """
    from pathspec import GitIgnoreSpec

    from filearr.tasks.scan import WalkAudit, walk

    _audit_tree(tmp_path)
    spec = GitIgnoreSpec.from_lines(["*.tmp", ".*", "node_modules/"])
    audit = WalkAudit(count_pruned=True)
    rels = {rel for _p, rel, _s, _m in walk(str(tmp_path), spec, audit=audit)}

    assert rels == {"keep.mkv"}
    # Recursive: buried.mkv + deep/a.js + deep/b.js.
    assert audit.pruned_files == 3
    on_disk = sum(1 for p in tmp_path.rglob("*") if p.is_file())
    assert on_disk == 6
    assert len(rels) + audit.excluded_filtered + audit.pruned_files == on_disk


async def test_staged_on_defers_once_at_end(env, tmp_path, monkeypatch):
    """Staged ON: NO defers happen mid-walk; the WHOLE library is deferred in a
    single end-of-scan pass."""
    import filearr.tasks.scan as scan_mod
    from filearr.config import get_settings
    from filearr.models import ScanRun

    monkeypatch.setattr(get_settings(), "staged_pipeline", True)
    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(env, root, name="on", enabled_categories=["image"])

    calls: list[list[str]] = []
    _stub_defers(monkeypatch, scan_mod, calls)

    async with env() as session:
        run = ScanRun(library_id=lib.id, stats={})
        session.add(run)
        await session.commit()
        stats = await scan_mod._scan_body(session, lib, run)

    assert stats["seen"] == 600
    assert len(calls) == 1, "exactly one end-of-scan defer, none mid-walk"
    assert len(calls[0]) == 600


async def test_staged_stopped_defers_seen_items(env, tmp_path, monkeypatch):
    """A gracefully-stopped staged scan still defers what it saw before stopping."""
    import filearr.tasks.scan as scan_mod
    from filearr.config import get_settings
    from filearr.models import ScanRun

    monkeypatch.setattr(get_settings(), "staged_pipeline", True)
    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(env, root, name="stop", enabled_categories=["image"])

    calls: list[list[str]] = []
    _stub_defers(monkeypatch, scan_mod, calls)

    async with env() as session:
        run = ScanRun(library_id=lib.id, stats={})
        session.add(run)
        await session.commit()
        _flip_after_first_commit(monkeypatch, session, run.id, "stopping")
        stats = await scan_mod._scan_body(session, lib, run)

    assert run.status == "stopped"
    assert stats.get("stopped") is True
    assert 0 < stats["seen"] < 600
    # end-of-walk defer still fires with the seen (partial) set.
    total = sum(len(c) for c in calls)
    assert total == stats["seen"] > 0


async def test_staged_cancelled_defers_nothing(env, tmp_path, monkeypatch):
    """A cancelled staged scan defers NOTHING; its committed strays keep
    quick_hash NULL so the next scan's self-heal re-queues them (crash-safety)."""
    from sqlalchemy import select

    import filearr.tasks.scan as scan_mod
    from filearr.config import get_settings
    from filearr.models import Item, ScanRun

    monkeypatch.setattr(get_settings(), "staged_pipeline", True)
    root = tmp_path / "lib"
    _touch(root, 600, "jpg")
    lib = await _mk_library(env, root, name="cancel", enabled_categories=["image"])

    calls: list[list[str]] = []
    _stub_defers(monkeypatch, scan_mod, calls)

    async with env() as session:
        run = ScanRun(library_id=lib.id, stats={})
        session.add(run)
        await session.commit()
        _flip_after_first_commit(monkeypatch, session, run.id, "cancelled")
        stats = await scan_mod._scan_body(session, lib, run)

    assert stats.get("aborted") is True
    assert calls == [], "cancelled scan defers no extraction"
    # Committed strays exist and are un-extracted (quick_hash NULL) -> self-heal.
    async with env() as s:
        rows = (
            await s.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars().all()
    assert len(rows) > 0
    assert all(i.quick_hash is None for i in rows)


# --------------------------------------------------------------------------- #
# Part 3 — extract reschedule gate                                            #
# --------------------------------------------------------------------------- #
def _file_facts(path: str):
    p = Path(path)
    return p.name, p.suffix.lstrip("."), p.stat().st_size  # sync (ASYNC240)


async def _mk_item(env, lib_id, path: str, media="other"):
    from filearr.file_groups import detect_category, detect_group
    from filearr.models import Item

    name, ext, size = _file_facts(path)
    async with env() as s:
        item = Item(
            library_id=lib_id,
            file_category=detect_category(path),
            file_group=detect_group(path),
            path=path,
            rel_path=name,
            filename=name,
            extension=ext,
            size=size,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def test_extract_reschedules_while_scan_walks(env, tmp_path, monkeypatch):
    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync
    from filearr.config import get_settings
    from filearr.models import ScanRun

    monkeypatch.setattr(get_settings(), "staged_pipeline", True)
    monkeypatch.setattr(extract_mod, "SessionLocal", env)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop)

    f = tmp_path / "f.bin"
    f.write_bytes(b"payload")
    lib = await _mk_library(env, tmp_path, name="gate")
    item_id = await _mk_item(env, lib.id, str(f))

    # A running scan on the SAME library -> extract must reschedule, not run.
    async with env() as s:
        s.add(ScanRun(library_id=lib.id, status="running", stats={}))
        await s.commit()

    with pytest.raises(extract_mod.RescheduleExtract):
        await extract_mod.extract_item(item_id)


async def test_extract_proceeds_when_no_scan_walking(env, tmp_path, monkeypatch):
    from sqlalchemy import select

    import filearr.tasks.extract as extract_mod
    import filearr.tasks.index_sync as index_sync
    from filearr.config import get_settings
    from filearr.models import Item

    monkeypatch.setattr(get_settings(), "staged_pipeline", True)
    monkeypatch.setattr(extract_mod, "SessionLocal", env)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop)

    f = tmp_path / "g.bin"
    f.write_bytes(b"payload-2")
    lib = await _mk_library(env, tmp_path, name="nogate")
    item_id = await _mk_item(env, lib.id, str(f))

    # No running scan -> extract proceeds and hashes the file (no raise).
    await extract_mod.extract_item(item_id)
    async with env() as s:
        item = (
            await s.execute(select(Item).where(Item.id == item_id))
        ).scalar_one()
    assert item.quick_hash is not None


async def test_staged_retry_strategy_reschedule_is_attempt_agnostic():
    """RescheduleExtract always yields a retry decision regardless of attempts;
    a genuine exception mirrors the old retry=2 budget."""
    from procrastinate.jobs import Job

    from filearr.tasks.extract import (
        EXTRACT_MAX_ATTEMPTS,
        RescheduleExtract,
        StagedExtractRetry,
    )

    strat = StagedExtractRetry()

    def _job(attempts):
        return Job(
            id=1,
            queue="extract",
            lock=None,
            queueing_lock=None,
            task_name="filearr.tasks.extract.extract_item",
            task_kwargs={},
            attempts=attempts,
        )

    # Gate: rescheduled even at a huge attempt count.
    d = strat.get_retry_decision(exception=RescheduleExtract(), job=_job(99))
    assert d is not None and d.retry_at is not None

    # Genuine failure: retried under budget, gives up at/after the ceiling.
    assert strat.get_retry_decision(exception=RuntimeError(), job=_job(0)) is not None
    assert (
        strat.get_retry_decision(
            exception=RuntimeError(), job=_job(EXTRACT_MAX_ATTEMPTS)
        )
        is None
    )
