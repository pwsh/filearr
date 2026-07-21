"""P10-T10 — GET /items/{id}/agent-status (hosting agent identity + online + verify).

Covers the new read endpoint: 404 unknown item; ``{"agent_hosted": false}`` for a
centrally-scanned item; the agent-hosted payload (name / status / online /
last_seen_at / last_verified_at / verify_in_flight); the online threshold window;
the active / revoked / pending lifecycle labels; and the ``verify_in_flight`` flag
when a pending verify command exists. RBAC visibility (404 out-of-scope) rides the
same ``authorize_item`` used by ``GET /items/{id}``.

Reuses the migrated-pgserver harness of ``test_verify_p10t3.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, grant_cache, rbac
from filearr import db as db_mod
from filearr import worker as worker_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Agent,
    AgentCommand,
    ItemStatus,
    Library,
    PathGrant,
    Principal,
    User,
)
from filearr.models import Item as ItemModel

pytestmark = pytest.mark.asyncio
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
            "agent_commands",
            "path_grants",
            "sessions",
            "security_events",
            "items",
            "libraries",
            "users",
            "principals",
            "agents",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(
    maker,
    *,
    agent_owned=True,
    last_seen_delta=timedelta(seconds=10),
    revoked=False,
    pending=False,
    verified_at=None,
):
    """Seed an agent + library + item. ``last_seen_delta`` = age of last_seen_at
    (None → never seen); ``pending`` → no cert bound; ``revoked`` → kill-switched."""
    now = datetime.now(UTC)
    async with maker() as s:
        agent = Agent(
            name="nas-01", hostname="nas", platform="linux",
            cert_fingerprint=None if pending else "FP:" + uuid.uuid4().hex,
            last_seen_at=None if last_seen_delta is None else now - last_seen_delta,
            revoked_at=now if revoked else None,
        )
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path="/data/media",
            source_agent_id=agent.id if agent_owned else None,
            agent_library_ref="/data/media" if agent_owned else None,
        )
        s.add(lib)
        await s.flush()
        item = ItemModel(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/media/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1000,
            mtime=now,
            status=ItemStatus.active,
            last_verified_at=verified_at,
            path_scope=rbac.path_to_ltree("x.mkv", library_id=lib.id),
        )
        s.add(item)
        await s.commit()
        return agent.id, lib.id, item.id


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def _get(c, item_id):
    return await c.get(f"/api/v1/items/{item_id}/agent-status")


# --------------------------------------------------------------------------- #
# Basic shapes                                                                 #
# --------------------------------------------------------------------------- #
async def test_unknown_item_404(client, maker):
    c, _ = client
    r = await _get(c, uuid.uuid4())
    assert r.status_code == 404


async def test_central_item_not_agent_hosted(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker, agent_owned=False)
    r = await _get(c, item_id)
    assert r.status_code == 200
    assert r.json() == {"agent_hosted": False}


async def test_agent_hosted_online_payload(client, maker):
    c, _ = client
    verified = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    agent_id, _lib, item_id = await _seed(maker, verified_at=verified)
    r = await _get(c, item_id)
    assert r.status_code == 200
    b = r.json()
    assert b["agent_hosted"] is True
    assert b["agent_id"] == str(agent_id)
    assert b["agent_name"] == "nas-01"
    assert b["agent_status"] == "active"
    assert b["online"] is True
    assert b["last_seen_at"] is not None
    assert b["last_verified_at"].startswith("2026-07-17T09:00")
    assert b["verify_in_flight"] is False


# --------------------------------------------------------------------------- #
# Online threshold                                                             #
# --------------------------------------------------------------------------- #
async def test_offline_when_last_seen_stale(client, maker):
    c, settings = client
    # last_seen well beyond the online window → offline.
    agent_id, _lib, item_id = await _seed(
        maker, last_seen_delta=timedelta(seconds=settings.agent_online_threshold_seconds + 60)
    )
    b = (await _get(c, item_id)).json()
    assert b["agent_status"] == "active"
    assert b["online"] is False


async def test_offline_when_never_seen(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker, last_seen_delta=None)
    b = (await _get(c, item_id)).json()
    assert b["online"] is False
    assert b["last_seen_at"] is None


# --------------------------------------------------------------------------- #
# Lifecycle labels                                                             #
# --------------------------------------------------------------------------- #
async def test_revoked_status(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker, revoked=True)
    b = (await _get(c, item_id)).json()
    assert b["agent_status"] == "revoked"


async def test_pending_status(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker, pending=True)
    b = (await _get(c, item_id)).json()
    assert b["agent_status"] == "pending"


# --------------------------------------------------------------------------- #
# verify_in_flight                                                             #
# --------------------------------------------------------------------------- #
async def test_verify_in_flight_true_for_pending_command(client, maker):
    c, _ = client
    agent_id, _lib, item_id = await _seed(maker)
    async with maker() as s:
        now = datetime.now(UTC)
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="rehash_check",
                item_id=item_id,
                payload={},
                status="pending",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await s.commit()
    b = (await _get(c, item_id)).json()
    assert b["verify_in_flight"] is True


async def test_verify_in_flight_false_for_terminal_command(client, maker):
    c, _ = client
    agent_id, _lib, item_id = await _seed(maker)
    async with maker() as s:
        now = datetime.now(UTC)
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="stat_check",
                item_id=item_id,
                payload={},
                status="done",
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await s.commit()
    b = (await _get(c, item_id)).json()
    assert b["verify_in_flight"] is False


# --------------------------------------------------------------------------- #
# RBAC visibility (auth on)                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def rbac_client(maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    grant_cache._cache.clear()
    grant_cache.bump_generation()
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def _mk_user(maker, username, role="user"):
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username=username.lower(),
                password_hash=authx.hash_password("pw-123456"),
                auth_provider="local",
            )
        )
        await s.commit()
        return p.id


async def _grant(maker, subject_id, library_id, action):
    async with maker() as s:
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=subject_id,
                library_id=library_id,
                scope=rbac.library_label(library_id),
                action=action,
                effect="allow",
            )
        )
        await s.commit()
    grant_cache.bump_generation()


async def _login(c, username):
    r = await c.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-123456"}
    )
    assert r.status_code == 200, r.text


async def test_rbac_invisible_item_404(rbac_client, maker):
    c, _ = rbac_client
    _agent, _lib, item_id = await _seed(maker)
    await _mk_user(maker, "nogrant")
    await _login(c, "nogrant")
    r = await _get(c, item_id)
    assert r.status_code == 404


async def test_rbac_visible_item_ok(rbac_client, maker):
    c, _ = rbac_client
    _agent, lib_id, item_id = await _seed(maker)
    uid = await _mk_user(maker, "reader")
    await _grant(maker, uid, lib_id, "search_metadata")
    await _login(c, "reader")
    r = await _get(c, item_id)
    assert r.status_code == 200
    assert r.json()["agent_hosted"] is True
