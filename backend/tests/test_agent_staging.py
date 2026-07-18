"""P10-T4 — staging_transfers + the resumable agent->central staging data plane.

Covers the model/migration insert, the attach idempotency + guards, the tus-subset
offset-PATCH wire protocol (HEAD offset, PATCH append, 409-on-mismatch, chunk/total
caps), the frozen state-machine transitions, resume-from-committed-offset, and the
FILEARR_AGENTS_ENABLED gate + agent-plane auth. Mirrors test_agent_commands's
harness (migrated pgserver Postgres + ASGI client + interim bearer auth).
"""

from __future__ import annotations

import hashlib
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
from filearr.models import Agent, AgentCommand, Item, Library, MediaType, StagingTransfer

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM staging_transfers"))
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_command(
    maker, *, kind: str = "stage_upload", status: str = "picked_up"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Seed an active agent + library + item + one command. Returns
    (agent_id, item_id, command_id, fingerprint)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add_all([agent, lib])
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            path="/data/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.flush()
        now = datetime.now(UTC)
        cmd = AgentCommand(
            agent_id=agent.id,
            kind=kind,
            item_id=item.id,
            payload={"library_ref": "/data", "rel_path": "x.mkv"},
            status=status,
            created_at=now,
            updated_at=now,
            picked_up_at=now,
            expires_at=now + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        return agent.id, item.id, cmd.id, fp


@pytest.fixture
async def client(db_maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
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
        yield c, maker, settings
    app.dependency_overrides.clear()


def _auth(fp: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fp}"}


# --------------------------------------------------------------------------- #
# Model / migration insert                                                     #
# --------------------------------------------------------------------------- #
async def test_model_insert_defaults(db_maker):
    agent_id, item_id, command_id, _ = await _seed_command(db_maker)
    async with db_maker() as s:
        t = StagingTransfer(
            item_id=item_id,
            agent_id=agent_id,
            command_id=command_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(t)
        await s.commit()
        row = (
            await s.execute(
                text(
                    "SELECT state, bytes_transferred, verified FROM staging_transfers LIMIT 1"
                )
            )
        ).one()
        assert row.state == "pending" and row.bytes_transferred == 0 and row.verified is False
    # UNIQUE(command_id): a second transfer for the same command is rejected.
    with pytest.raises(Exception):  # noqa: B017
        async with db_maker() as s:
            s.add(
                StagingTransfer(
                    item_id=item_id,
                    agent_id=agent_id,
                    command_id=command_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await s.commit()
    # state CHECK rejects junk.
    with pytest.raises(Exception):  # noqa: B017
        async with db_maker() as s:
            await s.execute(
                text(
                    "INSERT INTO staging_transfers (item_id,agent_id,command_id,state,expires_at)"
                    " VALUES (:i,:a,:c,'weird',now())"
                ).bindparams(i=item_id, a=agent_id, c=uuid.uuid4())
            )
            await s.commit()


# --------------------------------------------------------------------------- #
# Attach                                                                        #
# --------------------------------------------------------------------------- #
async def test_attach_creates_then_idempotent(client):
    c, maker, _ = client
    agent_id, item_id, command_id, fp = await _seed_command(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id), "total_bytes": 100},
        headers=_auth(fp),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "pending"
    assert body["bytes_transferred"] == 0
    assert body["total_bytes"] == 100
    assert body["item_id"] == str(item_id)
    assert r.headers["Upload-Offset"] == "0"
    tid = body["id"]

    # Re-attach: same row, 200.
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id), "total_bytes": 100},
        headers=_auth(fp),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == tid


async def test_attach_requires_picked_up_stage_upload(client):
    c, maker, _ = client
    # pending command -> not in-flight -> 409
    agent_id, _, command_id, fp = await _seed_command(maker, status="pending")
    r = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id)},
        headers=_auth(fp),
    )
    assert r.status_code == 409

    # wrong kind -> 409
    agent2, _, cmd2, fp2 = await _seed_command(maker, kind="stat_check")
    r2 = await c.post(
        f"/api/v1/agents/{agent2}/staging",
        json={"command_id": str(cmd2)},
        headers=_auth(fp2),
    )
    assert r2.status_code == 409


async def test_attach_gate_and_auth(client):
    c, maker, settings = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    # No bearer -> 401.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/staging", json={"command_id": str(command_id)}
    )
    assert r.status_code == 401
    # Agents disabled -> 404 (feature gate).
    settings.agents_enabled = False
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id)},
        headers=_auth(fp),
    )
    assert r2.status_code == 404
    settings.agents_enabled = True


# --------------------------------------------------------------------------- #
# Upload protocol — HEAD / PATCH                                                #
# --------------------------------------------------------------------------- #
async def _attach(c, agent_id, command_id, fp, total):
    r = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id), "total_bytes": total},
        headers=_auth(fp),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_full_upload_multi_chunk_stages(client):
    c, maker, settings = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    data = bytes(range(256)) * 40  # 10240 bytes
    tid = await _attach(c, agent_id, command_id, fp, len(data))

    offset = 0
    chunk = 4096
    while offset < len(data):
        piece = data[offset : offset + chunk]
        r = await c.patch(
            f"/api/v1/agents/{agent_id}/staging/{tid}",
            content=piece,
            headers={**_auth(fp), "Upload-Offset": str(offset)},
        )
        assert r.status_code == 200, r.text
        offset += len(piece)
        assert r.headers["Upload-Offset"] == str(offset)

    final = r.json()
    assert final["state"] == "staged"
    assert final["bytes_transferred"] == len(data)
    assert final["verified"] is False  # P10-T5 seam

    # HEAD reflects the committed offset.
    h = await c.head(f"/api/v1/agents/{agent_id}/staging/{tid}", headers=_auth(fp))
    assert h.status_code == 200
    assert h.headers["Upload-Offset"] == str(len(data))
    assert h.headers["Upload-State"] == "staged"

    # The staged file on disk matches the source bytes.
    staged = Path(settings.staging_dir) / f"{uuid.UUID(tid)}.bin"
    assert staged.read_bytes() == data
    assert hashlib.sha256(staged.read_bytes()).digest() == hashlib.sha256(data).digest()


async def test_offset_mismatch_returns_409_with_current(client):
    c, maker, _ = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    data = b"x" * 5000
    tid = await _attach(c, agent_id, command_id, fp, len(data))
    # Correct first chunk.
    r = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data[:2000],
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    assert r.status_code == 200 and r.json()["bytes_transferred"] == 2000
    # Wrong offset -> 409 with the current committed offset.
    bad = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data[2000:4000],
        headers={**_auth(fp), "Upload-Offset": "999"},
    )
    assert bad.status_code == 409
    assert bad.json() == {"reason": "offset_mismatch", "offset": 2000}
    assert bad.headers["Upload-Offset"] == "2000"
    # Retry at the corrected offset succeeds (idempotent).
    good = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data[2000:],
        headers={**_auth(fp), "Upload-Offset": "2000"},
    )
    assert good.status_code == 200 and good.json()["state"] == "staged"


async def test_chunk_past_total_refused(client):
    c, maker, _ = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    tid = await _attach(c, agent_id, command_id, fp, 100)
    r = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=b"y" * 200,  # past total_bytes=100
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    assert r.status_code == 409
    # bytes_transferred unchanged (still resumable from 0).
    h = await c.head(f"/api/v1/agents/{agent_id}/staging/{tid}", headers=_auth(fp))
    assert h.headers["Upload-Offset"] == "0"


async def test_chunk_too_large_413(client):
    c, maker, settings = client
    settings.staging_max_chunk_bytes = 1024
    agent_id, _, command_id, fp = await _seed_command(maker)
    tid = await _attach(c, agent_id, command_id, fp, 10_000)
    r = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=b"z" * 2048,
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    assert r.status_code == 413


async def test_empty_file_stages(client):
    c, maker, settings = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    tid = await _attach(c, agent_id, command_id, fp, 0)
    r = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=b"",
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    assert r.status_code == 200 and r.json()["state"] == "staged"
    staged = Path(settings.staging_dir) / f"{uuid.UUID(tid)}.bin"
    assert staged.read_bytes() == b""


async def test_completion_replay_idempotent(client):
    c, maker, _ = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    data = b"q" * 300
    tid = await _attach(c, agent_id, command_id, fp, len(data))
    await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data,
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    # A duplicate PATCH after completion is a 200 no-op (lost ack, agent retried).
    again = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data,
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    assert again.status_code == 200 and again.json()["state"] == "staged"


async def test_wrong_agent_transfer_404(client):
    c, maker, _ = client
    agent_id, _, command_id, fp = await _seed_command(maker)
    tid = await _attach(c, agent_id, command_id, fp, 100)
    # A different agent (its own fingerprint) cannot touch this transfer.
    other_id, _, _, other_fp = await _seed_command(maker)
    h = await c.head(
        f"/api/v1/agents/{other_id}/staging/{tid}", headers=_auth(other_fp)
    )
    assert h.status_code == 404
