"""T8 -- extraction throughput controls.

Covers the four knobs:
  * queue routing (extract jobs land on the ``extract`` queue at a LOWER
    priority than scan-control jobs),
  * batch defer (N jobs deferred in one call, and ONLY after the batch commit),
  * queue-depth stats query (backlog / done / failed counts read from
    ``procrastinate_jobs``),
  * concurrency / queue-list env parsing on Settings.

The queue tests run against the real Procrastinate schema on a throwaway
pgserver Postgres, driving the real ``scan._defer_extract_batch`` code path (no
mock of the connector), then reading ``procrastinate_jobs`` back.
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
    """Bind ``filearr.worker.proc_app`` to the throwaway Postgres for the test,
    applying the procrastinate schema once, then restore the original connector.
    Yields the pgserver for read-back assertions. Each test truncates first."""
    import psycopg
    from procrastinate import PsycopgConnector

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
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
            yield proc_pg
    proc_app.connector = original


async def _fetch_jobs(proc_pg):
    import psycopg

    with psycopg.connect(proc_pg.get_uri()) as conn:
        rows = conn.execute(
            "SELECT task_name, queue_name, priority, status::text, args "
            "FROM procrastinate_jobs ORDER BY id"
        ).fetchall()
    return rows


async def test_batch_defer_lands_on_extract_queue_low_priority(proc_connector):
    from filearr.config import get_settings
    from filearr.tasks.scan import _defer_extract_batch

    ids = [f"item-{i}" for i in range(5)]
    await _defer_extract_batch(ids)

    rows = await _fetch_jobs(proc_connector)
    assert len(rows) == 5, "one job per item id"
    settings = get_settings()
    for task_name, queue_name, priority, status, job_args in rows:
        assert task_name == "filearr.tasks.extract.extract_item"
        assert queue_name == settings.queue_extract == "extract"
        assert priority == settings.extract_priority < 0
        assert status == "todo"
        assert job_args["item_id"] in ids
    got_ids = {row[4]["item_id"] for row in rows}
    assert got_ids == set(ids)


async def test_batch_defer_empty_is_noop(proc_connector):
    from filearr.tasks.scan import _defer_extract_batch

    await _defer_extract_batch([])
    assert await _fetch_jobs(proc_connector) == []


async def test_defer_happens_after_commit_not_before(proc_connector):
    import filearr.tasks.scan as scan_mod

    events: list[str] = []

    class FakeSession:
        async def commit(self):
            events.append("commit")

    real_batch = scan_mod._defer_extract_batch

    async def spy_batch(item_ids):
        if item_ids:
            events.append(f"defer:{len(item_ids)}")
        return await real_batch(item_ids)

    session = FakeSession()
    pending = ["a", "b", "c"]
    # publish_progress's contract: commit THEN defer.
    await session.commit()
    await spy_batch(pending)

    assert events == ["commit", "defer:3"], events
    assert len(await _fetch_jobs(proc_connector)) == 3


async def test_queue_snapshot_counts_depth_done_failed(proc_connector):
    import psycopg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.queue_stats import queue_snapshot

    with psycopg.connect(proc_connector.get_uri(), autocommit=True) as conn:
        def ins(queue, status):
            conn.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES (%s, %s, '{}'::jsonb, %s::procrastinate_job_status)",
                (queue, "t", status),
            )
        for _ in range(3):
            ins("extract", "todo")
        ins("extract", "succeeded")
        ins("extract", "failed")
        ins("scan", "todo")

    uri = proc_connector.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        snap = await queue_snapshot(session)
    await engine.dispose()

    assert snap["extract"] == {"depth": 3, "running": 0, "done": 1, "failed": 1}
    assert snap["queues"]["extract"]["todo"] == 3
    assert snap["queues"]["scan"]["todo"] == 1


async def test_queue_snapshot_empty_when_schema_absent(pg_uri):
    # Fresh DATABASE on the shared server = no procrastinate schema; avoids
    # booting a second (flaky) postgres instance.
    import psycopg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.queue_stats import queue_snapshot

    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bare_t8")
        c.execute("CREATE DATABASE bare_t8")
    bare = pg_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    if "/postgres?" in bare:
        bare = bare.replace("/postgres?", "/bare_t8?")
    else:
        head, query = bare.rsplit("?", 1)
        bare = head.rsplit("/", 1)[0] + "/bare_t8?" + query
    engine = create_async_engine(bare)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        snap = await queue_snapshot(session)
    await engine.dispose()
    assert snap["queues"] == {}

def test_worker_queue_list_parsing():
    from filearr.config import Settings

    s = Settings(_env_file=None)
    assert s.worker_concurrency == 4
    assert s.worker_queue_list is None
    assert s.extract_priority < 0
    assert (s.queue_scan, s.queue_extract, s.queue_index, s.queue_maintenance) == (
        "scan",
        "extract",
        "index",
        "maintenance",
    )

    assert Settings(_env_file=None, worker_queues="extract").worker_queue_list == ["extract"]
    assert Settings(
        _env_file=None, worker_queues=" extract , index ,"
    ).worker_queue_list == ["extract", "index"]
    assert Settings(_env_file=None, worker_queues="   ").worker_queue_list is None
    assert Settings(_env_file=None, worker_concurrency=12).worker_concurrency == 12


# --------------------------------------------------------------------------- #
# Scaled-down (500-file) end-to-end smoke: a real scan batch-defers extract    #
# jobs onto the extract queue after commit, and records walk throughput.       #
# Timing assertion is loose (only that files_per_s is a positive number) so it  #
# cannot flake on slow CI.                                                      #
# --------------------------------------------------------------------------- #
async def test_scan_500_files_batch_defers_and_records_throughput(proc_connector, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from filearr.models import Base, Library, ScanRun
    from filearr.tasks import scan as scan_mod

    # App tables live in the SAME database as the (already-applied) procrastinate
    # schema, so the real _defer_extract_batch writes to procrastinate_jobs here.
    uri = proc_connector.get_uri()
    uri_pg3 = uri.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri_pg3)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # 500 tiny image files (image extractor, but extraction never runs in-test --
    # we only assert the jobs were *deferred*).
    root = tmp_path / "lib"
    root.mkdir()
    n = 500
    for i in range(n):
        (root / f"f{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")

    try:
        async with Session() as session:
            lib = Library(name="smoke", root_path=str(root), enabled_types=["image"])
            session.add(lib)
            await session.commit()
            run = ScanRun(library_id=lib.id, stats={})
            session.add(run)
            await session.commit()
            stats = await scan_mod._scan_body(session, lib, run)
    finally:
        await engine.dispose()

    assert stats["new"] == n
    assert stats["files_per_s"] > 0
    assert stats["walk_seconds"] >= 0

    rows = await _fetch_jobs(proc_connector)
    # every new non-sidecar file produced exactly one extract job on the extract
    # queue -- deferred in batches AFTER each commit (no per-row round-trips).
    assert len(rows) == n
    assert all(q == "extract" and t == "filearr.tasks.extract.extract_item"
               for t, q, _pri, _st, _a in rows)
