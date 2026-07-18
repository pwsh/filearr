"""UI-T10 — jobs-dashboard read-only introspection.

Covers:
  * ``running_jobs`` shape + args allowlist + cap + graceful empty on a bare DB,
  * item_id -> rel_path enrichment (single IN query),
  * ``jobs_summary`` composition (queues + running + failed_recent + meili +
    scans_running) with Meili mocked so the test needs no live Meilisearch.

Mirrors ``test_throughput_t8.py``: real Procrastinate schema on a throwaway
pgserver Postgres, direct INSERTs into ``procrastinate_jobs``, read-back.
"""

from __future__ import annotations

import pytest

procrastinate = pytest.importorskip("procrastinate")

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def proc_pg(module_db):
    return module_db


@pytest.fixture
async def proc_connector(proc_pg):
    """Bind proc_app to the throwaway PG, apply the procrastinate schema +
    the app tables (same DB), truncate between tests."""
    import psycopg
    from procrastinate import PsycopgConnector
    from sqlalchemy.ext.asyncio import create_async_engine

    from filearr.models import Base
    from filearr.worker import proc_app

    dsn = proc_pg.get_uri()
    connector = PsycopgConnector(conninfo=dsn)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            # App tables in the SAME db.
            engine = create_async_engine(
                dsn.replace("postgresql://", "postgresql+psycopg://", 1)
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
                conn.execute("TRUNCATE items, scan_runs, libraries CASCADE")
            yield proc_pg
    proc_app.connector = original


def _session_maker(proc_pg):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    uri = proc_pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _insert_job(conn, queue, task, status, args="{}"):
    conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
        "VALUES (%s, %s, %s::jsonb, %s::procrastinate_job_status) RETURNING id",
        (queue, task, args, status),
    )


async def test_running_jobs_shape_and_args_allowlist(proc_connector):
    import json

    import psycopg

    from filearr.jobs_stats import running_jobs

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        # A doing extract job with a mix of allowlisted + secret args.
        _insert_job(
            conn, "extract", "filearr.tasks.extract.extract_item", "doing",
            json.dumps({
                "item_id": "11111111-1111-1111-1111-111111111111",
                "scan_run_id": "22222222-2222-2222-2222-222222222222",
                "secret_token": "should-not-leak",
                "item_ids": ["a", "b", "c"],  # bulk kwarg must be dropped
            }),
        )
        # A todo job must NOT appear (only status='doing').
        _insert_job(conn, "extract", "filearr.tasks.extract.extract_item", "todo")

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        jobs = await running_jobs(session)
    await engine.dispose()

    assert len(jobs) == 1, "only the doing job is returned"
    job = jobs[0]
    assert job["task"] == "extract_item"  # short name
    assert job["queue"] == "extract"
    # allowlist: known refs kept, everything else dropped
    assert set(job["args"]) == {"item_id", "scan_run_id"}
    assert "secret_token" not in job["args"]
    assert "item_ids" not in job["args"]
    assert set(job) == {
        "id", "queue", "task", "args", "started_at", "seconds_running", "rel_path",
        "size", "library_name", "attempts", "retry_cap", "worker_id", "worker_alive",
        "stalled",
    }
    # FIX-12: extract carries a genuine-failure retry budget (EXTRACT_MAX_ATTEMPTS).
    assert job["retry_cap"] == 2


async def test_running_jobs_resolves_rel_path(proc_connector):
    import json
    import uuid
    from datetime import UTC, datetime

    import psycopg

    from filearr.jobs_stats import running_jobs
    from filearr.models import Item, Library, MediaType

    engine, Session = _session_maker(proc_connector)
    item_id = None
    async with Session() as session:
        lib = Library(name="lib1", root_path="/data")
        session.add(lib)
        await session.commit()
        it = Item(
            library_id=lib.id, media_type=MediaType.video, path="/data/a.mkv",
            rel_path="movies/a.mkv", filename="a.mkv", size=1,
            mtime=datetime.now(UTC),
        )
        session.add(it)
        await session.commit()
        item_id = str(it.id)

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        _insert_job(
            conn, "extract", "filearr.tasks.extract.extract_item", "doing",
            json.dumps({"item_id": item_id}),
        )
        # bogus item_id must not crash the IN query (invalid uuid ignored)
        _insert_job(
            conn, "extract", "filearr.tasks.extract.extract_item", "doing",
            json.dumps({"item_id": str(uuid.uuid4())}),
        )

    async with Session() as session:
        jobs = await running_jobs(session)
    await engine.dispose()

    by_item = {j["args"].get("item_id"): j for j in jobs}
    assert by_item[item_id]["rel_path"] == "movies/a.mkv"
    # `size` is attached from the same items query (free) for a size suffix in the UI.
    assert by_item[item_id]["size"] == 1


async def test_running_jobs_capped_at_50(proc_connector):
    import psycopg

    from filearr.jobs_stats import running_jobs

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        for _ in range(60):
            _insert_job(conn, "extract", "filearr.tasks.extract.extract_item", "doing")

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        jobs = await running_jobs(session)
    await engine.dispose()
    assert len(jobs) == 50


async def test_running_jobs_empty_when_schema_absent(pg_uri):
    # A fresh DATABASE on the shared server has no procrastinate schema —
    # no second postgres instance needed (booting one is flaky under VM load).
    import psycopg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.jobs_stats import running_jobs

    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bare_uit10")
        c.execute("CREATE DATABASE bare_uit10")
    bare = pg_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    if "/postgres?" in bare:
        bare = bare.replace("/postgres?", "/bare_uit10?")
    else:
        head, query = bare.rsplit("?", 1)
        bare = head.rsplit("/", 1)[0] + "/bare_uit10?" + query
    engine = create_async_engine(bare)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        jobs = await running_jobs(session)
    await engine.dispose()
    assert jobs == []

async def test_jobs_summary_composition(proc_connector, monkeypatch):
    import json

    import psycopg

    import filearr.jobs_stats as js
    from filearr.jobs_stats import jobs_summary
    from filearr.models import ScanRun

    # Seed a running scan (+ its library) and a couple of queue rows.
    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        from filearr.models import Library

        lib = Library(name="films", root_path="/data/films")
        session.add(lib)
        await session.commit()
        run = ScanRun(library_id=lib.id, status="running", stats={"seen": 42})
        session.add(run)
        await session.commit()

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        _insert_job(conn, "extract", "t", "todo")
        _insert_job(conn, "extract", "t", "todo")
        _insert_job(conn, "extract", "t", "failed")
        _insert_job(
            conn, "extract", "filearr.tasks.extract.extract_item", "doing",
            json.dumps({"item_id": "x"}),
        )

    # Mock Meili so no live server is needed.
    async def fake_meili(session):
        return {
            "healthy": True, "document_count": 10, "is_indexing": False,
            "postgres_active": 10, "drift": 0, "in_sync": True,
        }

    monkeypatch.setattr(js, "meili_snapshot", fake_meili)

    async with Session() as session:
        summary = await jobs_summary(session)
    await engine.dispose()

    assert set(summary) == {
        "queues", "extract", "running", "failed_recent", "meili", "scans_running",
        "stalled", "priorities", "staged_pipeline", "disk", "resources", "thumbs",
        "upcoming",
    }
    # resources always carries the cpu tile plus the io/net/db keys (each may be
    # null on a non-Linux host / failed DB probe, but the keys are always present).
    assert {"cpu", "io", "net", "db"} <= set(summary["resources"])
    # upcoming is a dict keyed by queue (empty here — no scheduled_at rows / crons).
    assert isinstance(summary["upcoming"], dict)
    # UI-T14: the summary now surfaces the per-queue default priorities + the
    # staged-pipeline flag the Jobs page renders.
    assert summary["priorities"]["extract"] == -10
    assert summary["staged_pipeline"] is True
    # Monitoring additions: the thumbs + exports queues now have a priority entry
    # (their cards were previously renderable but had no working stepper).
    assert summary["priorities"]["thumbs"] == -15
    assert summary["priorities"]["exports"] == -20
    # disk keeps the untouched {status, low} banner contract AND gains the full
    # per-path detail for the always-on space indicator.
    assert set(summary["disk"]) == {"status", "low", "paths"}
    # resources.cpu is always present (all-null on a host without getloadavg).
    assert set(summary["resources"]["cpu"]) == {
        "load1", "load5", "load15", "cores", "percent"
    }
    # thumbs monitor: whole-cache totals + the re-exposed thumbs queue snapshot.
    assert set(summary["thumbs"]) == {"generated", "bytes", "failed_jobs", "queue"}
    # No thumbnail_manifest rows were seeded -> zero totals, never a KeyError.
    assert summary["thumbs"]["generated"] == 0
    assert summary["thumbs"]["bytes"] == 0
    # The seeded doing extract job has a NULL worker_id (no worker row), so the
    # reaper's heartbeat net flags it stalled — surfaced in the summary rollup.
    assert summary["stalled"] == {"total": 1, "by_queue": {"extract": 1}}
    assert summary["running"][0]["stalled"] is True
    assert summary["running"][0]["worker_alive"] is False
    assert summary["extract"]["depth"] == 2
    assert summary["extract"]["failed"] == 1
    assert summary["extract"]["running"] == 1
    assert len(summary["running"]) == 1
    assert summary["running"][0]["task"] == "extract_item"
    assert len(summary["failed_recent"]) == 1
    assert summary["meili"]["in_sync"] is True
    assert len(summary["scans_running"]) == 1
    scan = summary["scans_running"][0]
    assert scan["library_name"] == "films"
    assert scan["stats"]["seen"] == 42
    assert scan["rel_path"] is None


async def test_cpu_load_computed(monkeypatch):
    """The coarse CPU-load indicator computes ``percent = 100*load1/cores`` from a
    known load + core count. Pure (no DB, no statvfs) so it runs cross-platform —
    ``raising=False`` lets it stub the POSIX-only ``os`` calls on a Windows host."""
    import os

    from filearr.jobs_stats import _cpu_load

    monkeypatch.setattr(os, "getloadavg", lambda: (4.0, 2.0, 1.0), raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)

    cpu = _cpu_load()
    assert cpu["load1"] == 4.0
    assert cpu["load5"] == 2.0
    assert cpu["load15"] == 1.0
    assert cpu["cores"] == 8
    assert cpu["percent"] == 50.0  # 100 * 4 / 8


async def test_cpu_load_overload_not_clamped(monkeypatch):
    """When the run queue is deeper than the core count, ``percent`` exceeds 100 —
    the backend reports it honestly (only the UI bar width is clamped)."""
    import os

    from filearr.jobs_stats import _cpu_load

    monkeypatch.setattr(os, "getloadavg", lambda: (11.0, 9.0, 7.0), raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)

    cpu = _cpu_load()
    assert cpu["percent"] == 137.5  # 100 * 11 / 8, unclamped


async def test_cpu_load_nulls_when_getloadavg_unavailable(monkeypatch):
    """On a host without ``os.getloadavg`` (Windows containers, restricted envs)
    every load field degrades to ``None`` and ``percent`` is ``None`` — the
    indicator never crashes the poll. ``cores`` is still best-effort known."""
    import os

    from filearr.jobs_stats import _cpu_load

    def _boom():
        raise OSError("getloadavg unavailable")

    monkeypatch.setattr(os, "getloadavg", _boom, raising=False)

    cpu = _cpu_load()
    assert cpu["load1"] is None
    assert cpu["load5"] is None
    assert cpu["load15"] is None
    assert cpu["percent"] is None  # unknown load -> no percent, never a crash
    assert set(cpu) == {"load1", "load5", "load15", "cores", "percent"}


async def test_thumbnail_totals_schema_absent(proc_connector):
    """``thumbnail_totals`` on a DB WITHOUT the ``thumbnail_manifest`` table (a
    fresh DB before init_db created the app tables) returns zeroed totals rather
    than raising — the Jobs ``thumbs`` monitor stays total on a bare database.

    Exercises the ``to_regclass`` guard directly by dropping the table on the
    throwaway DB (the function-scoped ``proc_connector`` fixture recreates it via
    ``Base.metadata.create_all`` for the next test). Avoids the POSIX-only statvfs
    path entirely, so it runs on a Windows host."""
    import psycopg

    from filearr.jobs_stats import thumbnail_totals

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS thumbnail_manifest CASCADE")

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        totals = await thumbnail_totals(session)
    await engine.dispose()
    assert totals == {"count": 0, "bytes": 0, "by_source": {}}


async def test_jobs_summary_monitoring_sections(proc_connector, monkeypatch):
    """The thumbs + resources + disk.paths monitoring sections compose correctly.

    ``monitored_statuses`` is mocked (so the assertions are deterministic AND this
    runs on a Windows host where ``os.statvfs`` is absent — it lands in the pass
    column rather than the known statvfs-error set). Thumbnail manifest is empty,
    so thumbs totals are zero; the thumbs ``queue`` re-exposes the thumbs-queue
    snapshot with its failed count surfaced at ``failed_jobs``."""
    import psycopg

    import filearr.jobs_stats as js
    from filearr import diskguard as dg
    from filearr.config import get_settings
    from filearr.jobs_stats import jobs_summary

    settings = get_settings()

    # Seed a couple of thumbs-queue rows (one failed) so failed_jobs is exercised.
    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        _insert_job(conn, settings.queue_thumbnail, "filearr.tasks.thumbs.thumb_item", "todo")
        _insert_job(conn, settings.queue_thumbnail, "filearr.tasks.thumbs.thumb_item", "failed")

    async def fake_meili(session):
        return {
            "healthy": True, "document_count": 0, "is_indexing": False,
            "postgres_active": 0, "drift": 0, "in_sync": True,
        }

    monkeypatch.setattr(js, "meili_snapshot", fake_meili)
    # Deterministic disk: one warn path + one ok path (avoids POSIX-only statvfs).
    monkeypatch.setattr(
        dg, "monitored_statuses",
        lambda s: [
            {
                "path": "/config/thumbnails", "label": "thumbnails", "is_pg": False,
                "exists": True, "total": 100 * dg.GB, "free": 8 * dg.GB,
                "used": 92 * dg.GB, "pct_free": 8.0, "dev": 1,
                "status": dg.WARN, "reason": "free 8GB < 20GB floor",
            },
            {
                "path": "/tmp", "label": "tmp", "is_pg": False, "exists": True,
                "total": 50 * dg.GB, "free": 40 * dg.GB, "used": 10 * dg.GB,
                "pct_free": 80.0, "dev": 2, "status": dg.OK, "reason": "ok",
            },
        ],
    )

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        summary = await jobs_summary(session)
    await engine.dispose()

    # disk: banner contract untouched (only the non-ok path in `low`), plus the
    # full per-path detail for the always-on space indicator.
    assert summary["disk"]["status"] == dg.WARN
    assert [p["label"] for p in summary["disk"]["low"]] == ["thumbnails"]
    assert {p["label"] for p in summary["disk"]["paths"]} == {"thumbnails", "tmp"}
    tmp_row = next(p for p in summary["disk"]["paths"] if p["label"] == "tmp")
    expected_path_keys = {
        "path", "free", "total", "used", "pct_free", "status", "reason", "is_pg"
    }
    assert expected_path_keys <= set(tmp_row)

    # each disk path row now carries the device-dedupe `members` list (one member
    # here since the two mocked paths are on distinct devices dev=1/dev=2).
    assert all("members" in p for p in summary["disk"]["paths"])

    # resources.cpu present with the full key set; io/net/db keys always present.
    assert set(summary["resources"]["cpu"]) == {
        "load1", "load5", "load15", "cores", "percent"
    }
    assert {"io", "net", "db"} <= set(summary["resources"])
    # db health tile against the real test PG: a dict with the documented shape
    # (io/net are None on this non-Linux host — that is expected and tile-hiding).
    db = summary["resources"]["db"]
    assert db is not None
    assert {
        "backends", "active", "idle_in_tx", "waiting", "longest_query_s",
        "longest_idle_in_tx_s", "cache_hit_ratio", "deadlocks", "temp_files",
        "temp_bytes", "xact_commit", "xact_rollback", "queue_backlog",
    } == set(db)
    # queue_backlog reuses the queue snapshot: the seeded thumbs 'todo' row counts.
    assert db["queue_backlog"] >= 1

    # thumbs monitor: empty manifest -> zero totals; queue re-exposed + failed count.
    thumbs = summary["thumbs"]
    assert thumbs["generated"] == 0
    assert thumbs["bytes"] == 0
    assert thumbs["failed_jobs"] == 1
    assert thumbs["queue"] == summary["queues"].get(settings.queue_thumbnail, {})
    assert thumbs["queue"].get("failed") == 1


# --------------------------------------------------------------------------- #
# v2 monitoring: device-dedupe, /proc parsers, DB health, upcoming.           #
# --------------------------------------------------------------------------- #

def test_disk_dedupe_same_device_merges():
    """Two watch paths on the SAME st_dev collapse to ONE row: joined label, worst
    status, first path, and a members list for the tooltip."""
    from filearr import diskguard as dg

    rows = dg.dedupe_by_device([
        {
            "path": "/config/thumbnails", "label": "thumbnails", "is_pg": False,
            "total": 100 * dg.GB, "free": 8 * dg.GB, "used": 92 * dg.GB,
            "pct_free": 8.0, "dev": 42, "status": dg.WARN, "reason": "low",
        },
        {
            "path": "/config/tmp", "label": "temp", "is_pg": False,
            "total": 100 * dg.GB, "free": 8 * dg.GB, "used": 92 * dg.GB,
            "pct_free": 8.0, "dev": 42, "status": dg.OK, "reason": "ok",
        },
    ])
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "thumbnails, temp"  # joined watch-role labels
    assert row["status"] == dg.WARN            # worst of the group
    assert row["reason"] == "low"              # reason of the worst member
    assert row["path"] == "/config/thumbnails"  # first path
    assert [m["label"] for m in row["members"]] == ["thumbnails", "temp"]


def test_disk_dedupe_distinct_devices_stay_separate():
    """Distinct st_dev values remain distinct rows (no merge)."""
    from filearr import diskguard as dg

    rows = dg.dedupe_by_device([
        {"path": "/a", "label": "a", "is_pg": False, "total": 1, "free": 1,
         "used": 0, "pct_free": 100.0, "dev": 1, "status": dg.OK, "reason": "ok"},
        {"path": "/b", "label": "b", "is_pg": False, "total": 1, "free": 1,
         "used": 0, "pct_free": 100.0, "dev": 2, "status": dg.OK, "reason": "ok"},
    ])
    assert [r["label"] for r in rows] == ["a", "b"]


def test_disk_dedupe_missing_device_never_merges():
    """A missing/zero `dev` (degraded path / non-POSIX host) is its OWN device, so
    two such paths never collapse onto a shared falsy key."""
    from filearr import diskguard as dg

    rows = dg.dedupe_by_device([
        {"path": "/a", "label": "a", "is_pg": False, "total": 1, "free": 1,
         "used": 0, "pct_free": 100.0, "dev": None, "status": dg.OK, "reason": "ok"},
        {"path": "/b", "label": "b", "is_pg": False, "total": 1, "free": 1,
         "used": 0, "pct_free": 100.0, "dev": 0, "status": dg.OK, "reason": "ok"},
    ])
    assert [r["label"] for r in rows] == ["a", "b"]


def test_diskstats_parser_sums_whole_disks_only():
    """`/proc/diskstats` parser sums sectors×512 over WHOLE devices only —
    partitions (sda1, nvme0n1p1) and loop/dm pseudo-devices are excluded."""
    from filearr.jobs_stats import _diskstats_bytes

    text = (
        "   8       0 sda 100 0 200 0 0 0 400 0 0 0 0\n"       # whole disk: rd=200 wr=400
        "   8       1 sda1 50 0 100 0 0 0 100 0 0 0 0\n"        # partition: excluded
        " 259       0 nvme0n1 1 0 10 0 0 0 20 0 0 0 0\n"        # whole nvme: rd=10 wr=20
        " 259       1 nvme0n1p1 1 0 5 0 0 0 5 0 0 0 0\n"        # nvme partition: excluded
        "   7       0 loop0 1 0 999 0 0 0 999 0 0 0 0\n"        # loop: excluded
    )
    io = _diskstats_bytes(text)
    assert io == {"read_bytes": (200 + 10) * 512, "write_bytes": (400 + 20) * 512}


def test_net_parser_sums_all_but_lo():
    """`/proc/net/dev` parser sums rx/tx over every interface except `lo`."""
    from filearr.jobs_stats import _net_bytes

    text = (
        "Inter-|   Receive                        |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets ...\n"
        "    lo: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"     # excluded
        "  eth0: 500 5 0 0 0 0 0 0 700 7 0 0 0 0 0 0\n"          # rx=500 tx=700
        "  eth1: 100 1 0 0 0 0 0 0 300 3 0 0 0 0 0 0\n"          # rx=100 tx=300
    )
    net = _net_bytes(text)
    assert net == {"rx_bytes": 600, "tx_bytes": 1000}


def test_proc_parsers_absent_file_returns_none(monkeypatch):
    """Off Linux / when the /proc file is unreadable, the parsers return None so
    the UI hides the tile."""
    from filearr.jobs_stats import _diskstats_bytes, _net_bytes

    def _boom(*a, **k):
        raise OSError("no /proc")

    monkeypatch.setattr("builtins.open", _boom)
    assert _diskstats_bytes() is None
    assert _net_bytes() is None


async def test_db_health_failure_returns_none():
    """_db_health wraps every failure to None (never breaks the summary)."""
    from filearr.jobs_stats import _db_health

    class _RaisingSession:
        async def execute(self, *a, **k):
            raise RuntimeError("permission denied")

    assert await _db_health(_RaisingSession(), 0) is None


async def test_upcoming_shape_caps_and_excludes_agents(proc_connector, monkeypatch):
    """`upcoming`: procrastinate scheduled_at rows are capped 3/queue; cron-derived
    next scans merge into the `scan` queue (agent-owned libraries EXCLUDED); report
    schedules feed the `exports` queue. Cron helper is monkeypatched deterministic."""
    from datetime import UTC, datetime, timedelta

    import psycopg

    from filearr import schedule
    from filearr.jobs_stats import _upcoming
    from filearr.models import Agent, Library, ReportSchedule

    fixed = datetime(2030, 1, 1, 4, 0, tzinfo=UTC)
    monkeypatch.setattr(schedule, "next_occurrence", lambda expr, now: fixed)

    engine, Session = _session_maker(proc_connector)
    async with Session() as session:
        agent = Agent(name="a1", hostname="h1", platform="linux")
        session.add(agent)
        await session.commit()
        # A normally-scanned library with a cron -> should appear in `scan`.
        session.add(Library(name="films", root_path="/d/films", scan_cron="0 4 * * *"))
        # An agent-owned library with a cron -> MUST be excluded from `scan`.
        session.add(Library(
            name="remote", root_path="/agent/root", scan_cron="0 4 * * *",
            source_agent_id=agent.id, agent_library_ref="ref1",
        ))
        session.add(ReportSchedule(
            name="weekly", canned_report_key="inventory", format="csv",
            cron="0 6 * * 1", enabled=True,
        ))
        await session.commit()

    # 5 future-scheduled todo jobs on one queue -> capped to 3.
    soon = datetime.now(UTC) + timedelta(hours=1)
    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        for _ in range(5):
            conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, "
                "scheduled_at) VALUES ('maintenance', 'filearr.worker.x', '{}'::jsonb, "
                "'todo'::procrastinate_job_status, %s)",
                (soon,),
            )

    async with Session() as session:
        upcoming = await _upcoming(session)
    await engine.dispose()

    # scheduled_at rows capped at 3 per queue.
    assert len(upcoming["maintenance"]) == 3
    # cron scan: exactly the non-agent library (agent-owned "remote" excluded).
    scan_labels = [e["label"] for e in upcoming["scan"]]
    assert scan_labels == ["films"]
    assert upcoming["scan"][0]["task"] == "scan_library"
    assert upcoming["scan"][0]["at"] == fixed.isoformat()
    # report schedule -> exports queue.
    assert [e["label"] for e in upcoming["exports"]] == ["weekly"]
    assert upcoming["exports"][0]["task"] == "run_report_export"


def test_next_occurrence_forward_and_invalid():
    """`schedule.next_occurrence` returns the next future occurrence (tz-aware UTC)
    and None for empty/invalid expressions."""
    from datetime import UTC, datetime

    from filearr.schedule import next_occurrence

    now = datetime(2030, 1, 1, 3, 30, tzinfo=UTC)
    nxt = next_occurrence("0 4 * * *", now)  # daily 04:00 -> same day 04:00
    assert nxt == datetime(2030, 1, 1, 4, 0, tzinfo=UTC)
    assert next_occurrence("", now) is None
    assert next_occurrence("not a cron", now) is None
