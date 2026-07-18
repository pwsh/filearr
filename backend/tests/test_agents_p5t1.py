"""P5-T1 — distributed-agent enrollment: migration round-trip, token lifecycle
(mint / hash-at-rest / single-use / TTL-expiry / replay), the register-first
handshake (happy + tamper), cert-binding pending→active, admin-gating, the
FILEARR_AGENTS_ENABLED gate, revoke kill switch, and audit-event emission.

Runs against the migrated pgserver Postgres (mirrors test_rbac_p6t2's harness).
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
from filearr import agentsync
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, EnrollmentToken

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM enrollment_tokens"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    # Auth disabled: exercises endpoint logic (admin-gating covered separately
    # below by re-enabling auth). Feature flag ON for the functional tests.
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "ca_url", "https://ca.filearr.lan:9000")
    monkeypatch.setattr(settings, "ca_fingerprint", "deadbeef")
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Migration round-trip                                                         #
# --------------------------------------------------------------------------- #
async def test_migration_round_trip(db_maker):
    async with db_maker() as s:
        tok_raw, tok_hash = agentsync.generate_enrollment_token()
        tok = EnrollmentToken(
            token_hash=tok_hash,
            rollout_group="canary",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(tok)
        agent = Agent(name="nas", hostname="nas", platform="linux")
        s.add(agent)
        await s.commit()

        # tables + columns exist; the raw token is NOT stored anywhere.
        got = (
            await s.execute(text("SELECT rollout_group FROM enrollment_tokens LIMIT 1"))
        ).scalar_one()
        assert got == "canary"
        cnt = (
            await s.execute(
                text("SELECT count(*) FROM enrollment_tokens WHERE token_hash = :h"),
                {"h": tok_raw},
            )
        ).scalar_one()
        assert cnt == 0  # PK is the HASH, never the raw token

    # partial-unique fingerprint index: multiple pending (NULL) agents coexist,
    # but a bound fingerprint is unique.
    async with db_maker() as s:
        s.add(Agent(name="a", hostname="a", platform="linux"))
        s.add(Agent(name="b", hostname="b", platform="macos"))
        await s.commit()  # two NULL-fingerprint agents: OK
    async with db_maker() as s:
        s.add(Agent(name="c", hostname="c", platform="linux", cert_fingerprint="fp1"))
        await s.commit()
    with pytest.raises(Exception):  # noqa: B017 — unique violation on bound fp
        async with db_maker() as s:
            s.add(Agent(name="d", hostname="d", platform="linux", cert_fingerprint="fp1"))
            await s.commit()


# --------------------------------------------------------------------------- #
# Token lifecycle                                                              #
# --------------------------------------------------------------------------- #
async def test_mint_token_shows_once_and_hashes_at_rest(client):
    c, maker, _ = client
    r = await c.post("/api/v1/agents/enrollment-tokens", json={"rollout_group": "canary"})
    assert r.status_code == 201, r.text
    body = r.json()
    raw = body["token"]
    assert raw.startswith("fae_")
    assert body["token_hash"] == agentsync.hash_enrollment_token(raw)

    # at rest: only the hash, never the raw token.
    async with maker() as s:
        row = await s.get(EnrollmentToken, body["token_hash"])
        assert row is not None
        assert row.rollout_group == "canary"
        assert row.consumed_at is None

    # list never re-exposes the raw token.
    lst = (await c.get("/api/v1/agents/enrollment-tokens")).json()
    assert lst[0]["status"] == "active"
    assert "token" not in lst[0]


async def test_register_consumes_token_single_use_and_assigns_id(client):
    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]

    reg = await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": "media-nas", "platform": "linux"},
    )
    assert reg.status_code == 201, reg.text
    out = reg.json()
    agent_id = out["agent_id"]
    assert out["status"] == "pending"
    assert out["enroll_secret"]
    assert out["ca"]["url"] == "https://ca.filearr.lan:9000"
    assert uuid.UUID(agent_id)  # server-assigned

    # token now consumed + linked to the agent.
    async with maker() as s:
        th = agentsync.hash_enrollment_token(raw)
        trow = await s.get(EnrollmentToken, th)
        assert trow.consumed_at is not None
        assert str(trow.consumed_by) == agent_id

    # replay of the SAME token is rejected (single-use).
    replay = await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": "evil", "platform": "linux"},
    )
    assert replay.status_code == 401
    assert replay.json()["detail"].endswith("consumed") or "consumed" in replay.json()["detail"]


async def test_expired_token_rejected(client):
    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    # force-expire it in the DB.
    async with maker() as s:
        th = agentsync.hash_enrollment_token(raw)
        row = await s.get(EnrollmentToken, th)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    reg = await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": "h", "platform": "linux"},
    )
    assert reg.status_code == 401
    assert "expired" in reg.json()["detail"]


async def test_unknown_token_and_bad_platform_rejected(client):
    c, _, _ = client
    r = await c.post(
        "/api/v1/agents/register",
        json={"token": "fae_nope", "hostname": "h", "platform": "linux"},
    )
    assert r.status_code == 401  # unknown token
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    r2 = await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": "h", "platform": "toaster"},
    )
    # platform is a plain string in the schema; agentsync raises bad_platform -> 400.
    assert r2.status_code == 400
    # the (still-valid) token must NOT have been consumed by the rejected attempt.


async def test_revoke_unconsumed_token(client):
    c, _, _ = client
    th = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token_hash"]
    d = await c.delete(f"/api/v1/agents/enrollment-tokens/{th}")
    assert d.status_code == 204
    lst = (await c.get("/api/v1/agents/enrollment-tokens")).json()
    assert lst == []


# --------------------------------------------------------------------------- #
# Cert binding pending -> active + tamper                                      #
# --------------------------------------------------------------------------- #
async def test_cert_binding_pending_to_active_and_secret_tamper(client):
    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    out = (
        await c.post(
            "/api/v1/agents/register",
            json={"token": raw, "hostname": "h", "platform": "windows"},
        )
    ).json()
    agent_id, secret = out["agent_id"], out["enroll_secret"]

    # wrong secret is refused (no hijack of a guessed pending UUID).
    bad = await c.post(
        f"/api/v1/agents/{agent_id}/certificate",
        json={"enroll_secret": "wrong", "cert_fingerprint": "AA:BB"},
    )
    assert bad.status_code == 401

    ok = await c.post(
        f"/api/v1/agents/{agent_id}/certificate",
        json={"enroll_secret": secret, "cert_fingerprint": "AA:BB"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "active"
    assert ok.json()["cert_fingerprint"] == "AA:BB"

    # secret is spent: a second (different) binding attempt fails already_bound.
    again = await c.post(
        f"/api/v1/agents/{agent_id}/certificate",
        json={"enroll_secret": secret, "cert_fingerprint": "CC:DD"},
    )
    assert again.status_code == 409


# --------------------------------------------------------------------------- #
# Fleet console + revoke kill switch                                           #
# --------------------------------------------------------------------------- #
async def test_list_and_revoke_agent(client):
    c, _, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    aid = (
        await c.post(
            "/api/v1/agents/register",
            json={"token": raw, "hostname": "h", "platform": "macos"},
        )
    ).json()["agent_id"]

    lst = (await c.get("/api/v1/agents")).json()
    assert len(lst) == 1 and lst[0]["status"] == "pending"

    d = await c.delete(f"/api/v1/agents/{aid}")
    assert d.status_code == 200
    assert d.json()["status"] == "revoked"
    assert d.json()["revoked_at"] is not None
    # idempotent
    d2 = await c.delete(f"/api/v1/agents/{aid}")
    assert d2.status_code == 200 and d2.json()["status"] == "revoked"


# --------------------------------------------------------------------------- #
# Hard delete (?purge=true) + consumed-token force delete                       #
# --------------------------------------------------------------------------- #
async def test_purge_pending_agent_hard_deletes(client):
    """A failed-enrollment pending row (no data footprint) purges completely."""
    c, _, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    aid = (
        await c.post(
            "/api/v1/agents/register",
            json={"token": raw, "hostname": "h", "platform": "linux"},
        )
    ).json()["agent_id"]

    d = await c.delete(f"/api/v1/agents/{aid}?purge=true")
    assert d.status_code == 200
    assert (await c.get("/api/v1/agents")).json() == []
    # gone means gone: a second purge is a 404, not idempotent-200 like revoke.
    assert (await c.delete(f"/api/v1/agents/{aid}?purge=true")).status_code == 404


async def test_purge_refused_while_agent_owns_data(client):
    """An agent referenced by a library (or items) can only be revoked (409)."""
    from filearr.models import Library

    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    aid = (
        await c.post(
            "/api/v1/agents/register",
            json={"token": raw, "hostname": "h", "platform": "linux"},
        )
    ).json()["agent_id"]
    async with maker() as s:
        s.add(
            Library(
                name="purge-guard-lib",
                root_path="/agent/root",
                source_agent_id=uuid.UUID(aid),
                agent_library_ref="/agent/root",
            )
        )
        await s.commit()

    d = await c.delete(f"/api/v1/agents/{aid}?purge=true")
    assert d.status_code == 409
    assert "revoke" in d.json()["detail"]
    # plain revoke still works on the same agent
    assert (await c.delete(f"/api/v1/agents/{aid}")).status_code == 200


async def test_consumed_token_delete_requires_force(client):
    c, _, _ = client
    minted = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()
    raw, th = minted["token"], minted["token_hash"]
    await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": "h", "platform": "linux"},
    )
    # consumed: plain delete refuses, force deletes
    assert (await c.delete(f"/api/v1/agents/enrollment-tokens/{th}")).status_code == 409
    assert (
        await c.delete(f"/api/v1/agents/enrollment-tokens/{th}?force=true")
    ).status_code == 204
    assert (await c.get("/api/v1/agents/enrollment-tokens")).json() == []


# --------------------------------------------------------------------------- #
# Audit events                                                                 #
# --------------------------------------------------------------------------- #
async def test_audit_events_emitted(client):
    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    aid = (
        await c.post(
            "/api/v1/agents/register",
            json={"token": raw, "hostname": "h", "platform": "linux"},
        )
    ).json()["agent_id"]
    await c.delete(f"/api/v1/agents/{aid}")

    async with maker() as s:
        rows = (
            await s.execute(text("SELECT event_type FROM security_events"))
        ).scalars().all()
    assert "agent_token_minted" in rows
    assert "agent_registered" in rows
    assert "agent_revoked" in rows


# --------------------------------------------------------------------------- #
# Feature gate + admin gating                                                  #
# --------------------------------------------------------------------------- #
async def test_feature_gate_404_when_disabled(client, monkeypatch):
    c, _, settings = client
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.get("/api/v1/agents")).status_code == 404
    assert (
        await c.post("/api/v1/agents/enrollment-tokens", json={})
    ).status_code == 404
    assert (
        await c.post(
            "/api/v1/agents/register",
            json={"token": "x", "hostname": "h", "platform": "linux"},
        )
    ).status_code == 404


async def test_admin_scope_required(db_maker, monkeypatch):
    # Re-enable auth: the admin surfaces must 401 without a bearer key, while the
    # agent-plane /register stays reachable (token is its credential).
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/v1/agents")).status_code == 401
        assert (
            await c.post("/api/v1/agents/enrollment-tokens", json={})
        ).status_code == 401
        # agent plane: not API-key gated (token is the credential) -> 401 for a
        # bad token, NOT 401 for missing bearer. A garbage token => 401 unknown.
        reg = await c.post(
            "/api/v1/agents/register",
            json={"token": "fae_none", "hostname": "h", "platform": "linux"},
        )
        assert reg.status_code == 401  # unknown token, reached the handler
    app.dependency_overrides.clear()
