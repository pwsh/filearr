"""W6-D3 extensible inventory framework (central-side): capability persistence on
poll, the inventory-results receiver (auth / cap / gzip roundtrip / wrong-agent /
write-if-absent / non-inventory), and an inline inventory command completion.

Runs against the migrated pgserver Postgres (mirrors test_agent_commands' harness).
"""

from __future__ import annotations

import gzip
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
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentCommand, Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker) -> tuple[uuid.UUID, uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add_all([agent, lib])
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return agent.id, item.id, fp


async def _mk_inventory_command(maker, agent_id, item_id) -> uuid.UUID:
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=agent_id,
            kind="inventory",
            item_id=item_id,
            payload={"preset": "user-documents", "collectors": ["stat"]},
            status="picked_up",
            picked_up_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        return cmd.id


@pytest.fixture
async def client(db_maker, tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "inventory_dir", str(tmp_path / "inventory"))
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings, tmp_path
    app.dependency_overrides.clear()


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


# --------------------------------------------------------------------------- #
# Capability persistence on poll                                              #
# --------------------------------------------------------------------------- #
async def test_poll_persists_capabilities(client):
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    caps = {
        "inventory_collectors": ["owner", "perms", "placeholder", "stat"],
        "inventory_version": 1,
    }
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "capabilities": caps},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities == caps

    # A subsequent poll WITHOUT capabilities leaves the stored value untouched.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities == caps


async def test_poll_oversize_capabilities_dropped_not_fatal(client):
    c, maker, settings, _ = client
    agent_id, _, fp = await _seed(maker)
    monkeypatch_cap = 64
    settings.agent_capabilities_max_bytes = monkeypatch_cap
    big = {"inventory_collectors": ["x" * 200], "inventory_version": 1}
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "capabilities": big},
        headers=_auth(fp),
    )
    # The poll still succeeds; the oversize advertisement is simply dropped.
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities is None


# --------------------------------------------------------------------------- #
# inventory-results receiver                                                   #
# --------------------------------------------------------------------------- #
def _gz(payload: bytes) -> bytes:
    return gzip.compress(payload)


async def test_inventory_results_gzip_roundtrip_and_idempotent(client):
    c, maker, _, tmp_path = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    blob = _gz(b'{"rel":"a.txt","size":1}\n{"rel":"b.txt","size":2}\n')

    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid), "Content-Type": "application/gzip"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["result_ref"] == f"inventory/{cid}.ndjson.gz" and body["created"] is True

    stored = tmp_path / "inventory" / f"{cid}.ndjson.gz"
    assert stored.exists()
    assert gzip.decompress(stored.read_bytes()).startswith(b'{"rel":"a.txt"')

    # Write-if-absent: a redelivered upload is a 200 no-op.
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid), "Content-Type": "application/gzip"},
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False


async def test_inventory_results_requires_auth(client):
    c, maker, _, _ = client
    agent_id, item_id, _ = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={"X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 401


async def test_inventory_results_wrong_agent_404(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    # A second agent cannot upload for the first agent's command.
    other_id, _, other_fp = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{other_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={**_auth(other_fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 404


async def test_inventory_results_non_inventory_command_409(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=agent_id,
            kind="stat_check",
            item_id=item_id,
            status="picked_up",
            picked_up_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        stat_cid = cmd.id
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={**_auth(fp), "X-Filearr-Command-Id": str(stat_cid)},
    )
    assert r.status_code == 409


async def test_inventory_results_rejects_non_gzip_415(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=b'{"rel":"plain"}\n',  # not gzip
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 415


async def test_inventory_results_size_cap_413(client):
    c, maker, settings, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    settings.agent_inventory_result_max_bytes = 16
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"x" * 1024),
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 413


async def test_inventory_results_missing_command_header_422(client):
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers=_auth(fp),
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Inline inventory command completion                                          #
# --------------------------------------------------------------------------- #
async def test_inventory_command_enqueue_poll_complete_inline(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    # Enqueue an inventory command via the EXISTING command-creation endpoint.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={
            "kind": "inventory",
            "item_id": str(item_id),
            "payload": {"preset": "downloads", "collectors": ["stat"], "max_entries": 100},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Payload passes through untouched.
    assert r.json()["payload"]["preset"] == "downloads"

    # Poll picks it up.
    poll = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert poll.status_code == 200
    assert any(cc["id"] == cid and cc["kind"] == "inventory" for cc in poll.json())

    # Complete with an inline summary + entries.
    result = {
        "summary": {"roots_expanded": 1, "entries": 2, "denied": 0},
        "entries": [{"rel": "a.txt", "size": 1}, {"rel": "b.txt", "size": 2}],
    }
    done = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": result},
        headers=_auth(fp),
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    assert done.json()["result"]["summary"]["entries"] == 2
