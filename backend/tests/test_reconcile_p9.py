"""P9-T7 — Postgres↔Meili reconciliation sweep.

Pure diff/cap planner tests plus integration tests driving ``run_reconcile_sweep``
against a throwaway pgserver Postgres with an in-memory fake Meili client. Covers:
count-match no-op, missing-doc repair, orphan deletion, busy-queue skip (real
``procrastinate_jobs`` SQL), cap + carry-over, and the ``/api/stats`` meili
snapshot. The sweep must NEVER write Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from filearr.tasks.reconcile import ReconcilePlan, plan_reconcile  # noqa: E402


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Fake Meili client (stateful, in-memory)                                     #
# --------------------------------------------------------------------------- #
class FakeStore:
    def __init__(self, ids=()):
        self.docs: dict[str, dict] = {i: {"id": i} for i in ids}


class FakeIndex:
    def __init__(self, store: FakeStore):
        self.store = store

    async def get_stats(self):
        return SimpleNamespace(number_of_documents=len(self.store.docs), is_indexing=False)

    async def get_documents(self, *, offset=0, limit=20, fields=None):
        rows = [self.store.docs[k] for k in sorted(self.store.docs)]
        page = rows[offset : offset + limit]
        results = [{"id": d["id"]} for d in page] if fields == ["id"] else page
        return SimpleNamespace(results=results, offset=offset, limit=limit, total=len(rows))

    async def update_documents(self, docs, primary_key=None):
        for d in docs:
            self.store.docs[d["id"]] = d

    async def delete_documents(self, ids):
        for i in ids:
            self.store.docs.pop(i, None)


class FakeClient:
    def __init__(self, store: FakeStore, healthy=True):
        self.store = store
        self.healthy = healthy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        return FakeIndex(self.store)

    async def health(self):
        return SimpleNamespace(status="available" if self.healthy else "unavailable")


# --------------------------------------------------------------------------- #
# Pure planner                                                                 #
# --------------------------------------------------------------------------- #
def test_plan_in_sync_is_empty():
    plan = plan_reconcile({"a", "b"}, {"a", "b"}, max_fixes=100)
    assert plan == ReconcilePlan([], [], 0, 0, 0)
    assert not plan.capped


def test_plan_detects_missing_and_orphans():
    pg = {"a", "b", "c"}
    meili = {"b", "c", "x"}  # missing: a ; orphan: x
    plan = plan_reconcile(pg, meili, max_fixes=100)
    assert plan.to_upsert == ["a"]
    assert plan.to_delete == ["x"]
    assert plan.missing_total == 1 and plan.orphan_total == 1
    assert plan.carried == 0


def test_plan_caps_and_carries_deletes_first():
    pg = {"a", "b"}  # both missing from meili
    meili = {"x", "y", "z"}  # all orphans
    plan = plan_reconcile(pg, meili, max_fixes=2)
    # orphan deletes are budgeted before missing upserts
    assert plan.to_delete == ["x", "y"]
    assert plan.to_upsert == []
    assert plan.missing_total == 2 and plan.orphan_total == 3
    # carried = (3-2 orphans) + (2-0 missing) = 3
    assert plan.carried == 3 and plan.capped


def test_plan_cap_spills_budget_into_upserts():
    pg = {"a", "b", "c"}  # missing from meili
    meili = {"x"}  # one orphan
    plan = plan_reconcile(pg, meili, max_fixes=2)
    assert plan.to_delete == ["x"]  # 1 delete
    assert plan.to_upsert == ["a"]  # 1 upsert (remaining budget)
    assert plan.carried == 2  # b, c deferred


# --------------------------------------------------------------------------- #
# Integration: real Postgres + fake Meili                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def recon_pg(module_db):
    return module_db


@pytest.fixture
async def recon(recon_pg, monkeypatch):
    import filearr.meili_stats as stats_mod
    import filearr.search as search_mod
    import filearr.tasks.reconcile as recon_mod
    from filearr.models import Base

    engine = create_async_engine(_psycopg3(recon_pg.get_uri()))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DROP TABLE IF EXISTS procrastinate_jobs"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(recon_mod, "SessionLocal", maker)

    store = FakeStore()

    def factory():
        return FakeClient(store)

    monkeypatch.setattr(search_mod, "client", factory)
    monkeypatch.setattr(recon_mod, "client", factory)
    monkeypatch.setattr(stats_mod, "client", factory)

    try:
        yield SimpleNamespace(maker=maker, store=store, engine=engine)
    finally:
        await engine.dispose()


async def _mk_library(maker):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name="lib", root_path="/d")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path, *, status="active"):
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="video", file_group="video",
            status=status,
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp4",
            size=1,
            mtime=datetime.now(UTC),
            metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _active_ids(maker):
    from sqlalchemy import select

    from filearr.models import Item, ItemStatus

    async with maker() as s:
        rows = (
            await s.execute(select(Item.id).where(Item.status == ItemStatus.active))
        ).scalars()
        return {str(r) for r in rows}


async def test_count_match_is_noop(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    b = await _mk_item(recon.maker, lib, "b.mp4")
    recon.store.docs = {a: {"id": a}, b: {"id": b}}

    out = await run_reconcile_sweep()
    assert out["status"] == "in_sync"
    assert out["postgres"] == out["meili"] == 2
    # store untouched
    assert set(recon.store.docs) == {a, b}


async def test_missing_doc_is_repaired(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    b = await _mk_item(recon.maker, lib, "b.mp4")
    # Meili is missing b entirely (a lost index_sync batch).
    recon.store.docs = {a: {"id": a}}

    out = await run_reconcile_sweep()
    assert out["status"] == "repaired"
    assert out["upserted"] == 1 and out["deleted"] == 0
    assert set(recon.store.docs) == {a, b}
    # the repaired doc is a full projection, not just an id stub
    assert recon.store.docs[b]["rel_path"] == "b.mp4"


async def test_orphan_doc_is_deleted(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    # a tombstoned item must NOT be indexed; its stale doc is an orphan.
    gone = await _mk_item(recon.maker, lib, "gone.mp4", status="missing")
    orphan = "deadbeef-0000-0000-0000-000000000000"
    recon.store.docs = {a: {"id": a}, gone: {"id": gone}, orphan: {"id": orphan}}

    out = await run_reconcile_sweep()
    assert out["status"] == "repaired"
    assert out["deleted"] == 2 and out["upserted"] == 0
    assert set(recon.store.docs) == {a}
    assert set(recon.store.docs) == await _active_ids(recon.maker)


async def test_cap_limits_work_and_carries_over(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    ids = [await _mk_item(recon.maker, lib, f"f{i}.mp4") for i in range(5)]
    recon.store.docs = {}  # everything missing

    out = await run_reconcile_sweep(max_fixes=2)
    assert out["status"] == "repaired"
    assert out["upserted"] == 2
    assert out["carried"] == 3 and out["capped"] is True
    assert len(recon.store.docs) == 2

    # next sweep drains more (carry-over converges)
    out2 = await run_reconcile_sweep(max_fixes=2)
    assert out2["upserted"] == 2 and len(recon.store.docs) == 4
    await run_reconcile_sweep(max_fixes=2)
    assert len(recon.store.docs) == 5
    assert set(recon.store.docs) == set(ids)


async def test_busy_index_queue_skips_sweep(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    await _mk_item(recon.maker, lib, "b.mp4")
    # Meili is out of sync, but the index queue has pending work -> skip to avoid
    # a false-alarm full diff while the backlog is legitimately draining.
    recon.store.docs = {a: {"id": a}}
    async with recon.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE procrastinate_jobs "
                "(id serial PRIMARY KEY, queue_name text, status text)"
            )
        )
        await conn.execute(
            text("INSERT INTO procrastinate_jobs (queue_name, status) VALUES ('index','todo')")
        )

    out = await run_reconcile_sweep()
    assert out == {"status": "skipped", "reason": "index_queue_busy"}
    # projection left untouched (sweep never ran)
    assert set(recon.store.docs) == {a}


async def test_non_index_queue_work_does_not_block(recon):
    from filearr.tasks.reconcile import run_reconcile_sweep

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    recon.store.docs = {a: {"id": a}}
    async with recon.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE procrastinate_jobs "
                "(id serial PRIMARY KEY, queue_name text, status text)"
            )
        )
        # extract-queue backlog + a finished index job must NOT block the sweep
        await conn.execute(
            text("INSERT INTO procrastinate_jobs (queue_name, status) VALUES ('extract','todo')")
        )
        await conn.execute(
            text("INSERT INTO procrastinate_jobs (queue_name, status) VALUES ('index','succeeded')")
        )

    out = await run_reconcile_sweep()
    assert out["status"] == "in_sync"


async def test_meili_snapshot_reports_drift(recon):
    from filearr.meili_stats import meili_snapshot

    lib = await _mk_library(recon.maker)
    a = await _mk_item(recon.maker, lib, "a.mp4")
    await _mk_item(recon.maker, lib, "b.mp4")
    recon.store.docs = {a: {"id": a}}  # meili has 1, postgres has 2

    async with recon.maker() as s:
        snap = await meili_snapshot(s)
    assert snap["healthy"] is True
    assert snap["document_count"] == 1
    assert snap["postgres_active"] == 2
    assert snap["drift"] == 1
    assert snap["in_sync"] is False


async def test_meili_snapshot_total_when_meili_down(recon, monkeypatch):
    import filearr.meili_stats as stats_mod
    from filearr.meili_stats import meili_snapshot

    lib = await _mk_library(recon.maker)
    await _mk_item(recon.maker, lib, "a.mp4")

    def down_factory():
        raise RuntimeError("meili unreachable")

    monkeypatch.setattr(stats_mod, "client", down_factory)
    async with recon.maker() as s:
        snap = await meili_snapshot(s)
    assert snap["healthy"] is False
    assert snap["document_count"] is None
    assert snap["drift"] is None and snap["in_sync"] is None
    assert snap["postgres_active"] == 1  # Postgres side still reported
