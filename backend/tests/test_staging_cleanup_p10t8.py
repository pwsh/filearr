"""P10-T8 — staging TTL cleanup sweep.

Drives ``filearr.staging_sweep.run_staging_cleanup_sweep`` against a migrated
Postgres, simulating each reclaim schedule via direct row-timestamp manipulation:

  * an unclaimed staged file past ``expires_at`` is reaped (row -> expired, file
    deleted, command expired);
  * a staged file being ACTIVELY downloaded (recent ``last_range_request_at``) is
    NOT cut mid-stream, even past its TTL;
  * an abandoned PARTIAL upload (uploading, no progress, TTL still in the future)
    is reclaimed early on its shorter schedule;
  * a healthy in-progress upload (recent ``updated_at``, TTL in the future) is
    left untouched.

Harness mirrors test_transfers_p10t13 / test_agent_staging (migrated pgserver
Postgres + async sessionmaker).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Agent, AgentCommand, Item, Library, StagingTransfer
from filearr.staging_sweep import run_staging_cleanup_sweep

BACKEND_DIR = Path(__file__).resolve().parent.parent

GRACE = 3600  # download grace seconds
ABANDONED = 21600  # abandoned-partial seconds


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in ("staging_transfers", "agent_commands", "items", "libraries", "agents"):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _seed(maker) -> dict:
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8], root_path="/agentroot",
            source_agent_id=agent.id,
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category="video", file_group="video",
            path="/agentroot/m.mkv", rel_path="m.mkv", filename="m.mkv",
            size=10, mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return {"agent_id": agent.id, "library_id": lib.id, "item_id": item.id}


async def _mk_transfer(
    maker,
    tmp_path,
    ids: dict,
    *,
    state: str,
    expires_at: datetime,
    updated_at: datetime,
    last_range_request_at: datetime | None = None,
    cmd_status: str = "picked_up",
    write_file: bool = True,
) -> tuple[uuid.UUID, Path]:
    tid = uuid.uuid4()
    staged = Path(tmp_path) / f"{tid}.bin"
    if write_file:
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"data")
    now = datetime.now(UTC)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=ids["agent_id"], kind="stage_upload", item_id=ids["item_id"],
            payload={"library_ref": "/agentroot", "rel_path": "m.mkv"},
            status=cmd_status, created_at=now, updated_at=now,
            picked_up_at=now if cmd_status != "pending" else None,
            expires_at=now + timedelta(hours=6),
        )
        s.add(cmd)
        await s.flush()
        t = StagingTransfer(
            id=tid, item_id=ids["item_id"], agent_id=ids["agent_id"],
            command_id=cmd.id, state=state, bytes_transferred=4, total_bytes=10,
            staged_path=str(staged), expires_at=expires_at,
            last_range_request_at=last_range_request_at,
            created_at=now, updated_at=updated_at,
        )
        s.add(t)
        await s.commit()
    return tid, staged


async def _state(maker, tid) -> tuple[str, str]:
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        cmd = await s.get(AgentCommand, t.command_id)
        return t.state, cmd.status


async def test_unclaimed_staged_past_ttl_reaped(maker, tmp_path):
    ids = await _seed(maker)
    now = datetime.now(UTC)
    tid, staged = await _mk_transfer(
        maker, tmp_path, ids, state="staged",
        expires_at=now - timedelta(minutes=5),  # past TTL
        updated_at=now - timedelta(hours=1),
    )
    async with maker() as s:
        res = await run_staging_cleanup_sweep(
            s, now=now, download_grace_seconds=GRACE, abandoned_upload_seconds=ABANDONED
        )
    assert res["reaped"] == 1 and res["ttl_expired"] == 1
    assert res["commands_expired"] == 1
    st, cmd_st = await _state(maker, tid)
    assert st == "expired"
    assert cmd_st == "expired"
    assert not staged.exists()  # file reclaimed


async def test_active_download_not_cut(maker, tmp_path):
    ids = await _seed(maker)
    now = datetime.now(UTC)
    tid, staged = await _mk_transfer(
        maker, tmp_path, ids, state="staged",
        expires_at=now - timedelta(minutes=5),  # past TTL...
        updated_at=now - timedelta(hours=1),
        last_range_request_at=now - timedelta(seconds=30),  # ...but downloading NOW
    )
    async with maker() as s:
        res = await run_staging_cleanup_sweep(
            s, now=now, download_grace_seconds=GRACE, abandoned_upload_seconds=ABANDONED
        )
    assert res["reaped"] == 0 and res["skipped_active"] == 1
    st, _ = await _state(maker, tid)
    assert st == "staged"
    assert staged.exists()  # not cut mid-stream


async def test_abandoned_partial_reclaimed_early(maker, tmp_path):
    ids = await _seed(maker)
    now = datetime.now(UTC)
    tid, staged = await _mk_transfer(
        maker, tmp_path, ids, state="uploading",
        expires_at=now + timedelta(hours=3),  # TTL still in the FUTURE
        updated_at=now - timedelta(seconds=ABANDONED + 60),  # no progress > threshold
    )
    async with maker() as s:
        res = await run_staging_cleanup_sweep(
            s, now=now, download_grace_seconds=GRACE, abandoned_upload_seconds=ABANDONED
        )
    assert res["reaped"] == 1 and res["abandoned"] == 1 and res["ttl_expired"] == 0
    st, cmd_st = await _state(maker, tid)
    assert st == "expired"
    assert cmd_st == "expired"
    assert not staged.exists()


async def test_healthy_upload_untouched(maker, tmp_path):
    ids = await _seed(maker)
    now = datetime.now(UTC)
    tid, staged = await _mk_transfer(
        maker, tmp_path, ids, state="uploading",
        expires_at=now + timedelta(hours=3),
        updated_at=now - timedelta(seconds=60),  # progressed a minute ago
    )
    async with maker() as s:
        res = await run_staging_cleanup_sweep(
            s, now=now, download_grace_seconds=GRACE, abandoned_upload_seconds=ABANDONED
        )
    assert res["reaped"] == 0 and res["skipped_active"] == 0
    st, _ = await _state(maker, tid)
    assert st == "uploading"
    assert staged.exists()
