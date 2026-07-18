"""P10-T6/T7 — transfer SSE progress stream + the active-item race backstop.

Covers the ``GET /transfers/{id}/events`` SSE surface:

  * a ``pending`` retrieve whose command was never picked up emits a ``progress``
    frame with ``waiting_for_agent=true`` (P10-T7 offline-agent normal case);
  * a ``pending`` retrieve whose command IS picked up reports
    ``waiting_for_agent=false``;
  * an ``expired`` transfer whose command was never picked up emits a terminal
    ``offline_timeout`` frame then ``done`` (P10-T7 lapsed-TTL failure);
  * a ``failed`` transfer emits a terminal ``error`` frame then ``done``;
  * a ``downloaded`` transfer emits ``done``;
  * an unknown id emits ``error`` and closes.

Plus the P10-T6 race backstop: the partial-unique index rejects a second ACTIVE
transfer for one item at the DB.

Harness mirrors test_transfers_p10t13 (migrated pgserver Postgres + httpx ASGI).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api import transfers as transfers_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentCommand, Item, Library, MediaType, StagingTransfer

BACKEND_DIR = Path(__file__).resolve().parent.parent


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


@pytest.fixture
async def client(maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    # Fast SSE poll so a background terminal-flip is observed quickly.
    monkeypatch.setattr(transfers_mod, "_SSE_POLL_INTERVAL", 0.05)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "staging_dir", str(tmp_path / "staging"))
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _seed_item(maker) -> dict:
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
            library_id=lib.id, media_type=MediaType.video,
            path="/agentroot/m.mkv", rel_path="m.mkv", filename="m.mkv",
            size=100, mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return {"agent_id": agent.id, "library_id": lib.id, "item_id": item.id}


async def _seed_transfer(
    maker, ids, *, state: str, cmd_status: str, picked_up: bool, verified: bool = False
) -> uuid.UUID:
    tid = uuid.uuid4()
    now = datetime.now(UTC)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=ids["agent_id"], kind="stage_upload", item_id=ids["item_id"],
            payload={"library_ref": "/agentroot", "rel_path": "m.mkv"},
            status=cmd_status, created_at=now, updated_at=now,
            picked_up_at=now if picked_up else None,
            expires_at=now + timedelta(hours=6),
        )
        s.add(cmd)
        await s.flush()
        t = StagingTransfer(
            id=tid, item_id=ids["item_id"], agent_id=ids["agent_id"],
            command_id=cmd.id, state=state, bytes_transferred=10, total_bytes=100,
            verified=verified, expires_at=now + timedelta(hours=6),
            created_at=now, updated_at=now,
        )
        s.add(t)
        await s.commit()
    return tid


async def _read_events(resp) -> list[tuple[str, dict]]:
    """Parse an SSE byte stream (aiter_text + ``\\n\\n`` framing, the proven scan-SSE
    pattern) into ``(event, data)`` tuples until a terminal ``done``/``error``."""
    events: list[tuple[str, dict]] = []
    buf = ""

    async def _pump():
        nonlocal buf
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                lines = [ln for ln in raw.split("\n") if ln]
                if all(ln.startswith(":") for ln in lines):  # keepalive ping
                    continue
                ev = "message"
                data_parts: list[str] = []
                for ln in lines:
                    if ln.startswith("event:"):
                        ev = ln[len("event:"):].strip()
                    elif ln.startswith("data:"):
                        data_parts.append(ln[len("data:"):].strip())
                events.append((ev, json.loads("\n".join(data_parts))))
                # ``done`` is the always-last terminal frame. An ``error`` frame
                # may PRECEDE ``done`` (failed transfer) or stand alone (unknown
                # id — the stream then closes on its own, ending aiter_text).
                if ev == "done":
                    return

    await asyncio.wait_for(_pump(), timeout=15.0)
    return events


async def _collect(c, url) -> list[tuple[str, dict]]:
    async with c.stream("GET", url) as resp:
        assert resp.status_code == 200
        return await _read_events(resp)


async def _flip_terminal(maker, tid, *, delay=0.2, state="downloaded"):
    """Background task: drive a live transfer terminal after a beat so the SSE
    stream emits its terminal frame and closes (mirrors the scan-SSE test)."""
    await asyncio.sleep(delay)
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        t.state = state
        await s.commit()


# --------------------------------------------------------------------------- #
# SSE                                                                           #
# --------------------------------------------------------------------------- #
async def test_progress_waiting_for_agent(client):
    c, maker = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, ids, state="pending", cmd_status="pending", picked_up=False
    )
    task = asyncio.create_task(_flip_terminal(maker, tid))
    events = await _collect(c, f"/api/v1/transfers/{tid}/events")
    await task
    progress = [d for e, d in events if e == "progress"]
    assert progress and progress[0]["waiting_for_agent"] is True
    assert progress[0]["state"] == "pending"
    assert events[-1][0] == "done"


async def test_progress_not_waiting_once_picked_up(client):
    c, maker = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, ids, state="pending", cmd_status="picked_up", picked_up=True
    )
    task = asyncio.create_task(_flip_terminal(maker, tid))
    events = await _collect(c, f"/api/v1/transfers/{tid}/events")
    await task
    progress = [d for e, d in events if e == "progress"]
    assert progress and progress[0]["waiting_for_agent"] is False


async def test_offline_timeout_terminal(client):
    c, maker = client
    ids = await _seed_item(maker)
    # expired while the command was NEVER picked up -> offline_timeout.
    tid = await _seed_transfer(
        maker, ids, state="expired", cmd_status="expired", picked_up=False
    )
    events = await _collect(c, f"/api/v1/transfers/{tid}/events")
    names = [e[0] for e in events]
    assert "offline_timeout" in names
    done = events[-1]
    assert done[0] == "done"
    assert done[1]["reason"] == "offline_timeout"


async def test_failed_terminal_emits_error(client):
    c, maker = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, ids, state="failed", cmd_status="done", picked_up=True
    )
    events = await _collect(c, f"/api/v1/transfers/{tid}/events")
    names = [e[0] for e in events]
    assert "error" in names
    assert events[-1][0] == "done"
    assert events[-1][1]["reason"] == "failed"


async def test_downloaded_terminal_done(client):
    c, maker = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, ids, state="downloaded", cmd_status="done", picked_up=True, verified=True
    )
    events = await _collect(c, f"/api/v1/transfers/{tid}/events")
    assert events[-1][0] == "done"
    assert events[-1][1]["reason"] == "downloaded"


async def test_unknown_transfer_error(client):
    c, _ = client
    events = await _collect(c, f"/api/v1/transfers/{uuid.uuid4()}/events")
    assert events[0][0] == "error"
    assert "not found" in events[0][1]["detail"]


# --------------------------------------------------------------------------- #
# P10-T6 race backstop: partial-unique index                                   #
# --------------------------------------------------------------------------- #
async def test_active_item_partial_unique_index(client):
    c, maker = client
    ids = await _seed_item(maker)
    await _seed_transfer(
        maker, ids, state="uploading", cmd_status="picked_up", picked_up=True
    )
    # A SECOND active (pending) transfer for the same item violates the partial
    # unique index (uq_staging_transfers_active_item).
    now = datetime.now(UTC)
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError under the driver
        async with maker() as s:
            cmd = AgentCommand(
                agent_id=ids["agent_id"], kind="stage_upload", item_id=ids["item_id"],
                payload={}, status="pending", created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=6),
            )
            s.add(cmd)
            await s.flush()
            s.add(
                StagingTransfer(
                    id=uuid.uuid4(), item_id=ids["item_id"], agent_id=ids["agent_id"],
                    command_id=cmd.id, state="pending", expires_at=now + timedelta(hours=6),
                )
            )
            await s.commit()
    # But a TERMINAL transfer for the same item does NOT block a new active one.
    async with maker() as s:
        await s.execute(
            text("UPDATE staging_transfers SET state='downloaded'")
        )
        await s.commit()
    now2 = datetime.now(UTC)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=ids["agent_id"], kind="stage_upload", item_id=ids["item_id"],
            payload={}, status="pending", created_at=now2, updated_at=now2,
            expires_at=now2 + timedelta(hours=6),
        )
        s.add(cmd)
        await s.flush()
        s.add(
            StagingTransfer(
                id=uuid.uuid4(), item_id=ids["item_id"], agent_id=ids["agent_id"],
                command_id=cmd.id, state="pending", expires_at=now2 + timedelta(hours=6),
            )
        )
        await s.commit()  # no error -- terminal row left the index
