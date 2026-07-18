"""QH-T4 — small-file re-hash sweep (worker.rehash_small_files).

Asserts the sweep re-enqueues exactly the active, <=128 KiB, old-scheme,
non-agent-owned items through the normal extract path, respects the per-tick
rate limit, and converges (an item re-stamped to the current scheme drops out).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Agent, Item, Library, MediaType
from filearr.provenance import _SCHEME

BACKEND_DIR = Path(__file__).resolve().parent.parent
OLD_SCHEME = "cfg1"  # the pre-QH-T4 scheme; current is _SCHEME (cfg2)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _mk_item(s, lib_id, name, *, size, policy_version, status="active"):
    item = Item(
        library_id=lib_id,
        media_type=MediaType.other,
        status=status,
        path=f"/root/{name}",
        rel_path=name,
        filename=name,
        extension="bin",
        size=size,
        mtime=datetime.now(UTC),
        policy_version=policy_version,
        quick_hash="q",
    )
    s.add(item)
    await s.flush()
    return str(item.id)


async def _run_sweep(maker, monkeypatch):
    """Run rehash_small_files_now with SessionLocal bound to the test DB, the
    extract-defer stubbed to capture ids, and proc_app.open_async neutralised."""
    import filearr.tasks.scan as scan_mod
    from filearr import db as db_mod
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    captured: list[str] = []

    async def _capture(item_ids):
        captured.extend(item_ids)

    monkeypatch.setattr(scan_mod, "_defer_extract_batch", _capture)

    @contextlib.asynccontextmanager
    async def _noop_open():
        yield

    monkeypatch.setattr(worker_mod.proc_app, "open_async", _noop_open)

    result = await worker_mod.rehash_small_files_now()
    return result, captured


async def test_sweep_targets_only_small_old_scheme_non_agent(maker, monkeypatch):
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint="FP1")
        s.add(agent)
        await s.flush()
        local = Library(name="local", root_path="/root")
        agent_lib = Library(
            name="agentlib", root_path="/aroot",
            source_agent_id=agent.id, agent_library_ref="/aroot",
        )
        s.add_all([local, agent_lib])
        await s.flush()

        old = f"{OLD_SCHEME}:abc"
        want = await _mk_item(s, local.id, "small_old.bin", size=100_000, policy_version=old)
        want_boundary = await _mk_item(
            s, local.id, "boundary.bin", size=131_072, policy_version=old
        )
        # Excluded: already on the current scheme.
        await _mk_item(s, local.id, "small_new.bin", size=100_000, policy_version=f"{_SCHEME}:a")
        # Excluded: never extracted (NULL policy_version) — normal extract handles it.
        await _mk_item(s, local.id, "small_null.bin", size=100_000, policy_version=None)
        # Excluded: >128 KiB (outside the affected band).
        await _mk_item(s, local.id, "large_old.bin", size=200_000, policy_version=old)
        # Excluded: trashed/missing (not active).
        await _mk_item(
            s, local.id, "trashed_old.bin", size=100_000, policy_version=old, status="trashed"
        )
        # Excluded: agent-owned (architect ruling — central can't open agent files).
        await _mk_item(s, agent_lib.id, "agent_small_old.bin", size=100_000, policy_version=old)
        await s.commit()

    result, captured = await _run_sweep(maker, monkeypatch)

    assert set(captured) == {want, want_boundary}
    assert result["requeued"] == 2


async def test_sweep_respects_batch_limit_and_converges(maker, monkeypatch):
    from filearr import worker as worker_mod

    monkeypatch.setattr(worker_mod, "REHASH_SWEEP_BATCH", 3)
    async with maker() as s:
        lib = Library(name="local", root_path="/root")
        s.add(lib)
        await s.flush()
        ids = []
        for i in range(5):
            ids.append(
                await _mk_item(s, lib.id, f"f{i}.bin", size=1000, policy_version=f"{OLD_SCHEME}:x")
            )
        await s.commit()

    # First tick: capped at the batch size.
    result, captured = await _run_sweep(maker, monkeypatch)
    assert result["requeued"] == 3
    assert len(set(captured)) == 3

    # Simulate the re-extract advancing those 3 to the current scheme.
    async with maker() as s:
        await s.execute(
            update(Item)
            .where(Item.id.in_(captured))
            .values(policy_version=f"{_SCHEME}:done")
        )
        await s.commit()

    # Second tick: the remaining 2 are picked up; the first 3 have dropped out.
    result2, captured2 = await _run_sweep(maker, monkeypatch)
    assert result2["requeued"] == 2
    assert set(captured2).isdisjoint(set(captured))

    # Advance the second batch too (their re-extract completes).
    async with maker() as s:
        await s.execute(
            update(Item)
            .where(Item.id.in_(captured2))
            .values(policy_version=f"{_SCHEME}:done")
        )
        await s.commit()

    # Third tick: fully converged, nothing left.
    result3, captured3 = await _run_sweep(maker, monkeypatch)
    assert result3["requeued"] == 0
    assert captured3 == []
