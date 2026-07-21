"""P10-T13 — RBAC-gated transfer API (live).

Covers initiate (202 + command/staging rows; RBAC gate before row creation; 422
non-agent-hosted; 409 revoked agent; 409 duplicate active transfer), status,
download (verified-only, Range-capable, watermark + staged→downloaded flip,
unconditional audit), and cancel (cleans up file + command, idempotent-ish).

Harness mirrors test_agent_staging.py / test_rbac_enforcement_p6t4.py (migrated
pgserver Postgres + httpx ASGI app; session-cookie principals for the RBAC path).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, grant_cache, rbac
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Agent,
    AgentCommand,
    Item,
    Library,
    PathGrant,
    Principal,
    SecurityEvent,
    StagingTransfer,
    User,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "staging_transfers",
            "agent_commands",
            "security_events",
            "path_grants",
            "sessions",
            "items",
            "libraries",
            "users",
            "principals",
            "agents",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    grant_cache._cache.clear()
    grant_cache.bump_generation()
    yield m
    await engine.dispose()


@pytest.fixture
async def client(maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
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


# --------------------------------------------------------------------------- #
# Seed helpers                                                                  #
# --------------------------------------------------------------------------- #
async def _seed_item(
    maker,
    *,
    agent_hosted: bool = True,
    revoked: bool = False,
    size: int = 1000,
    rel: str = "movie.mkv",
    set_scope: bool = False,
) -> dict:
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path="/agentroot",
            source_agent_id=agent.id if agent_hosted else None,
            agent_library_ref="/agentroot" if agent_hosted else None,
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path=f"/agentroot/{rel}",
            rel_path=rel,
            filename=rel.rsplit("/", 1)[-1],
            size=size,
            mtime=datetime.now(UTC),
            path_scope=rbac.path_to_ltree(rel, library_id=lib.id) if set_scope else None,
        )
        s.add(item)
        await s.commit()
        return {"agent_id": agent.id, "library_id": lib.id, "item_id": item.id}


def _write_staged(staging_dir: str, tid: uuid.UUID, data: bytes) -> Path:
    """Sync FS write of a staged body (kept off the async call graph)."""
    d = Path(staging_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{tid}.bin"
    p.write_bytes(data)
    return p


async def _seed_transfer(
    maker,
    settings,
    ids: dict,
    *,
    state: str,
    verified: bool,
    data: bytes | None = None,
) -> uuid.UUID:
    """Seed a staging transfer (+ its stage_upload command) directly, writing the
    staged file to disk when ``data`` is given."""
    tid = uuid.uuid4()
    now = datetime.now(UTC)
    staged_path = None
    if data is not None:
        staged_path = _write_staged(settings.staging_dir, tid, data)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=ids["agent_id"],
            kind="stage_upload",
            item_id=ids["item_id"],
            payload={"library_ref": "/agentroot", "rel_path": "movie.mkv"},
            status="picked_up",
            created_at=now,
            updated_at=now,
            picked_up_at=now,
            expires_at=now + timedelta(hours=6),
        )
        s.add(cmd)
        await s.flush()
        t = StagingTransfer(
            id=tid,
            item_id=ids["item_id"],
            agent_id=ids["agent_id"],
            command_id=cmd.id,
            state=state,
            bytes_transferred=len(data) if data is not None else 0,
            total_bytes=len(data) if data is not None else None,
            staged_path=str(staged_path) if staged_path else None,
            verified=verified,
            expires_at=now + timedelta(hours=6),
            created_at=now,
        )
        s.add(t)
        await s.commit()
    return tid


async def _count(maker, model) -> int:
    async with maker() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _mk_user(maker, username, role="user", password="pw-123456"):
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username=username.lower(),
                password_hash=authx.hash_password(password),
                auth_provider="local",
            )
        )
        await s.commit()
        return p.id


async def _grant(maker, subject_id, library_id, rel, action, effect="allow"):
    async with maker() as s:
        scope = (
            rbac.library_label(library_id)
            if not rel
            else rbac.path_to_ltree(rel, library_id=library_id)
        )
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=subject_id,
                library_id=library_id,
                scope=scope,
                action=action,
                effect=effect,
            )
        )
        await s.commit()
    grant_cache.bump_generation()


async def _login(c, username, password="pw-123456"):
    r = await c.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Initiate                                                                      #
# --------------------------------------------------------------------------- #
async def test_initiate_creates_command_and_transfer(client):
    c, maker, _ = client
    ids = await _seed_item(maker, size=4242)
    r = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r.status_code == 202, r.text
    body = r.json()
    tid = body["transfer_id"]
    assert body["state"] == "pending"

    async with maker() as s:
        cmd = (
            await s.execute(
                select(AgentCommand).where(AgentCommand.item_id == ids["item_id"])
            )
        ).scalar_one()
        assert cmd.kind == "stage_upload"
        assert cmd.status == "pending"
        assert cmd.payload["library_ref"] == "/agentroot"
        assert cmd.payload["rel_path"] == "movie.mkv"
        assert cmd.payload["verify"] is True  # default verify_hash
        t = await s.get(StagingTransfer, uuid.UUID(tid))
        assert t.command_id == cmd.id
        assert t.state == "pending"
        assert t.total_bytes == 4242
        assert t.staged_path and t.staged_path.endswith(f"{tid}.bin")
        # Unconditional initiate audit (audit_reads default OFF).
        n = (
            await s.execute(
                select(func.count()).select_from(SecurityEvent).where(
                    SecurityEvent.event_type == "agent_transfer_initiated"
                )
            )
        ).scalar_one()
        assert n == 1


async def test_initiate_non_agent_hosted_422(client):
    c, maker, _ = client
    ids = await _seed_item(maker, agent_hosted=False)
    r = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r.status_code == 422
    assert await _count(maker, StagingTransfer) == 0
    assert await _count(maker, AgentCommand) == 0


async def test_initiate_revoked_agent_409(client):
    c, maker, _ = client
    ids = await _seed_item(maker, revoked=True)
    r = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r.status_code == 409
    assert await _count(maker, StagingTransfer) == 0


async def test_initiate_unknown_item_404(client):
    c, _, _ = client
    r = await c.post(f"/api/v1/items/{uuid.uuid4()}/transfer", json={})
    assert r.status_code == 404


async def test_initiate_duplicate_active_transfer_409(client):
    c, maker, _ = client
    ids = await _seed_item(maker)
    r1 = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r1.status_code == 202
    existing_id = r1.json()["transfer_id"]
    r2 = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r2.status_code == 409
    assert existing_id in r2.json()["detail"]  # existing transfer id surfaced
    # Only ONE transfer/command created.
    assert await _count(maker, StagingTransfer) == 1
    assert await _count(maker, AgentCommand) == 1


# --------------------------------------------------------------------------- #
# RBAC gate                                                                     #
# --------------------------------------------------------------------------- #
async def test_initiate_rbac_denied_creates_no_rows(client, maker, monkeypatch):
    c, _, settings = client
    monkeypatch.setattr(settings, "auth_enabled", True)
    ids = await _seed_item(maker, set_scope=True)
    uid = await _mk_user(maker, "denied", role="user")
    # readable but NO download grant -> 403, and provably no side effects.
    await _grant(maker, uid, ids["library_id"], "", "search_metadata", "allow")
    await _login(c, "denied")

    before_cmd = await _count(maker, AgentCommand)
    before_t = await _count(maker, StagingTransfer)
    r = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r.status_code == 403
    assert await _count(maker, AgentCommand) == before_cmd
    assert await _count(maker, StagingTransfer) == before_t


async def test_initiate_rbac_allowed(client, maker, monkeypatch):
    c, _, settings = client
    monkeypatch.setattr(settings, "auth_enabled", True)
    ids = await _seed_item(maker, set_scope=True)
    uid = await _mk_user(maker, "mover", role="user")
    await _grant(maker, uid, ids["library_id"], "", "download", "allow")
    await _login(c, "mover")
    r = await c.post(f"/api/v1/items/{ids['item_id']}/transfer", json={})
    assert r.status_code == 202, r.text
    assert await _count(maker, StagingTransfer) == 1


# --------------------------------------------------------------------------- #
# Status                                                                        #
# --------------------------------------------------------------------------- #
async def test_status_reports_progress(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, settings, ids, state="uploading", verified=False, data=b"x" * 500
    )
    r = await c.get(f"/api/v1/transfers/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "uploading"
    assert body["verified"] is False
    assert body["bytes_transferred"] == 500
    assert body["total_bytes"] == 500
    assert "expires_at" in body


async def test_status_unknown_404(client):
    c, _, _ = client
    r = await c.get(f"/api/v1/transfers/{uuid.uuid4()}")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Download                                                                      #
# --------------------------------------------------------------------------- #
async def test_download_before_verified_409(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(
        maker, settings, ids, state="uploading", verified=False, data=b"y" * 100
    )
    r = await c.get(f"/api/v1/transfers/{tid}/download")
    assert r.status_code == 409
    assert "uploading" in r.json()["detail"].lower()


async def test_download_failed_verification_409(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    tid = await _seed_transfer(maker, settings, ids, state="failed", verified=False)
    r = await c.get(f"/api/v1/transfers/{tid}/download")
    assert r.status_code == 409
    assert "verification" in r.json()["detail"].lower()


async def test_download_verified_streams_and_flips_to_downloaded(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    data = bytes(range(256)) * 20  # 5120 bytes
    tid = await _seed_transfer(
        maker, settings, ids, state="staged", verified=True, data=data
    )
    r = await c.get(f"/api/v1/transfers/{tid}/download")
    assert r.status_code == 200, r.text
    assert r.content == data
    assert r.headers["content-disposition"].startswith("attachment")
    assert "movie.mkv" in r.headers["content-disposition"]
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        assert t.state == "downloaded"  # covered the final byte
        assert t.last_range_request_at is not None
        n = (
            await s.execute(
                select(func.count()).select_from(SecurityEvent).where(
                    SecurityEvent.event_type == "agent_transfer_downloaded"
                )
            )
        ).scalar_one()
        assert n == 1  # unconditional download audit


async def test_download_range_resume_then_final(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    data = bytes(range(256)) * 10  # 2560 bytes
    tid = await _seed_transfer(
        maker, settings, ids, state="staged", verified=True, data=data
    )
    # Partial (not covering the final byte) -> 206, stays staged.
    r1 = await c.get(
        f"/api/v1/transfers/{tid}/download", headers={"Range": "bytes=0-999"}
    )
    assert r1.status_code == 206
    assert r1.content == data[:1000]
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        assert t.state == "staged"
        assert t.last_range_request_at is not None
    # Remainder (covers the final byte) -> 206, flips to downloaded.
    r2 = await c.get(
        f"/api/v1/transfers/{tid}/download", headers={"Range": "bytes=1000-"}
    )
    assert r2.status_code == 206
    assert r1.content + r2.content == data
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        assert t.state == "downloaded"
    # Re-download after downloaded is still allowed (until TTL).
    r3 = await c.get(f"/api/v1/transfers/{tid}/download")
    assert r3.status_code == 200
    assert r3.content == data


# --------------------------------------------------------------------------- #
# Cancel                                                                        #
# --------------------------------------------------------------------------- #
async def test_cancel_cleans_up_file_and_command(client):
    c, maker, settings = client
    ids = await _seed_item(maker)
    data = b"z" * 300
    tid = await _seed_transfer(
        maker, settings, ids, state="staged", verified=True, data=data
    )
    staged = Path(settings.staging_dir) / f"{tid}.bin"
    assert staged.exists()

    r = await c.delete(f"/api/v1/transfers/{tid}")
    assert r.status_code == 200
    assert r.json()["state"] == "expired"
    assert not staged.exists()  # staged bytes reclaimed
    async with maker() as s:
        t = await s.get(StagingTransfer, tid)
        assert t.state == "expired"
        cmd = await s.get(AgentCommand, t.command_id)
        assert cmd.status == "cancelled"

    # Cancelling an already-terminal transfer -> 409.
    r2 = await c.delete(f"/api/v1/transfers/{tid}")
    assert r2.status_code == 409


async def test_cancel_unknown_404(client):
    c, _, _ = client
    r = await c.delete(f"/api/v1/transfers/{uuid.uuid4()}")
    assert r.status_code == 404
