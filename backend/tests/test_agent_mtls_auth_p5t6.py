"""P5-T6 — agent-plane mTLS-header authentication modes.

Exercises ``_authenticate_agent`` across the ``FILEARR_AGENT_AUTH_MODE`` matrix
(fingerprint / mtls-header / both) via the poll endpoint (the simplest agent-plane
endpoint — a 200 with an empty list proves auth passed). Covers: bearer works in
fingerprint/both, mtls-header identity == SAN (renewal-proof), bad shared secret,
SAN mismatch, missing proxy headers, bearer refused in mtls-header mode, the
optional fingerprint secondary check, and revoked-agent refusal.

Mirrors test_agent_commands.py's harness (migrated pgserver Postgres).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parent.parent
_SECRET = "proxy-shared-secret-under-test"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_agent(maker, *, fingerprint: str | None = "fp-" + uuid.uuid4().hex) -> uuid.UUID:
    """Create an active agent with an (optionally) bound cert fingerprint."""
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux", cert_fingerprint=fingerprint
        )
        s.add(agent)
        await s.commit()
        return agent.id


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    # default; individual tests override per-mode.
    monkeypatch.setattr(settings, "agent_auth_mode", "fingerprint")
    monkeypatch.setattr(settings, "proxy_shared_secret", _SECRET)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


def _bearer(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


def _mtls(agent_id, *, secret=_SECRET, san="__self__", fp: str | None = None) -> dict:
    h = {"X-Filearr-Proxy-Auth": secret}
    if san is not None:
        h["X-Filearr-Agent-San"] = str(agent_id) if san == "__self__" else san
    if fp is not None:
        h["X-Filearr-Agent-Fp"] = fp
    return h


async def _poll(c, agent_id, headers):
    return await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=headers
    )


# --------------------------------------------------------------------------- #
# fingerprint mode (default, interim) — unchanged bearer behaviour             #
# --------------------------------------------------------------------------- #
async def test_fingerprint_mode_bearer_ok(client):
    c, maker, settings = client
    settings.agent_auth_mode = "fingerprint"
    fp = "fp-" + uuid.uuid4().hex
    agent_id = await _seed_agent(maker, fingerprint=fp)
    assert (await _poll(c, agent_id, _bearer(fp))).status_code == 200
    # wrong bearer -> 401
    assert (await _poll(c, agent_id, _bearer("nope"))).status_code == 401
    # mtls headers are IGNORED in fingerprint mode (no bearer -> 401)
    assert (await _poll(c, agent_id, _mtls(agent_id))).status_code == 401


# --------------------------------------------------------------------------- #
# mtls-header mode                                                             #
# --------------------------------------------------------------------------- #
async def test_mtls_mode_san_identity_ok(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    # identity is the SAN; works even with NO fingerprint header (renewal-proof)
    agent_id = await _seed_agent(maker, fingerprint="bound-fp")
    r = await _poll(c, agent_id, _mtls(agent_id))  # san==agent_id, no fp header
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_mtls_mode_bad_shared_secret(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    agent_id = await _seed_agent(maker)
    r = await _poll(c, agent_id, _mtls(agent_id, secret="wrong-secret"))
    assert r.status_code == 401


async def test_mtls_mode_san_mismatch(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    agent_id = await _seed_agent(maker)
    # a valid cert, but SAN is a DIFFERENT agent id -> 403
    r = await _poll(c, agent_id, _mtls(agent_id, san=str(uuid.uuid4())))
    assert r.status_code == 403


async def test_mtls_mode_missing_headers(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    fp = "fp-" + uuid.uuid4().hex
    agent_id = await _seed_agent(maker, fingerprint=fp)
    # no proxy headers at all -> 401
    assert (await _poll(c, agent_id, {})).status_code == 401
    # proxy secret present but no SAN header -> 403 (authenticated proxy, no identity)
    assert (await _poll(c, agent_id, _mtls(agent_id, san=None))).status_code == 403


async def test_mtls_mode_bearer_refused(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    fp = "fp-" + uuid.uuid4().hex
    agent_id = await _seed_agent(maker, fingerprint=fp)
    # the (previously valid) bearer alone must NOT authenticate in mtls-header mode
    assert (await _poll(c, agent_id, _bearer(fp))).status_code == 401


async def test_mtls_mode_fingerprint_secondary_check(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    agent_id = await _seed_agent(maker, fingerprint="the-bound-fp")
    # matching fp header -> ok
    assert (await _poll(c, agent_id, _mtls(agent_id, fp="the-bound-fp"))).status_code == 200
    # contradicting fp header (agent HAS a bound fp) -> 403
    assert (await _poll(c, agent_id, _mtls(agent_id, fp="a-different-fp"))).status_code == 403


async def test_mtls_mode_fp_check_skipped_when_agent_unbound(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    # agent has NO bound fingerprint (e.g. pre-fingerprint enrollment): SAN alone
    # authenticates, an incoming fp header is not compared.
    agent_id = await _seed_agent(maker, fingerprint=None)
    assert (await _poll(c, agent_id, _mtls(agent_id, fp="whatever"))).status_code == 200


async def test_mtls_mode_revoked_agent(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    agent_id = await _seed_agent(maker)
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        a.revoked_at = datetime.now(UTC)
        await s.commit()
    assert (await _poll(c, agent_id, _mtls(agent_id))).status_code == 403


async def test_mtls_mode_unconfigured_secret_fails_closed(client):
    c, maker, settings = client
    settings.agent_auth_mode = "mtls-header"
    settings.proxy_shared_secret = None  # not configured
    agent_id = await _seed_agent(maker)
    # even an empty/blank proxy-auth header must not authenticate
    assert (await _poll(c, agent_id, _mtls(agent_id, secret=""))).status_code == 401


# --------------------------------------------------------------------------- #
# both mode (transition)                                                       #
# --------------------------------------------------------------------------- #
async def test_both_mode_prefers_mtls_when_headers_present(client):
    c, maker, settings = client
    settings.agent_auth_mode = "both"
    agent_id = await _seed_agent(maker, fingerprint="bound-fp")
    # proxy headers present + valid -> mtls path -> 200
    assert (await _poll(c, agent_id, _mtls(agent_id))).status_code == 200


async def test_both_mode_mtls_hard_fails_on_bad_secret(client):
    c, maker, settings = client
    settings.agent_auth_mode = "both"
    fp = "fp-" + uuid.uuid4().hex
    agent_id = await _seed_agent(maker, fingerprint=fp)
    # proxy-auth header PRESENT but wrong -> hard 401, NO silent downgrade to bearer
    headers = {**_mtls(agent_id, secret="wrong"), **_bearer(fp)}
    assert (await _poll(c, agent_id, headers)).status_code == 401


async def test_both_mode_falls_back_to_bearer(client):
    c, maker, settings = client
    settings.agent_auth_mode = "both"
    fp = "fp-" + uuid.uuid4().hex
    agent_id = await _seed_agent(maker, fingerprint=fp)
    # no proxy header at all -> bearer path -> 200
    assert (await _poll(c, agent_id, _bearer(fp))).status_code == 200
    # and a bad bearer with no proxy header -> 401
    assert (await _poll(c, agent_id, _bearer("nope"))).status_code == 401
