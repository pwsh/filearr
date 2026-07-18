"""P5-T2 (central half) — step-ca JWK one-time token (``ca_ott``) minting.

Covers the mint contract the (toolchain-blocked) Go agent will be written
against: a decode-and-verify round-trip of the ES256 OTT against the configured
public JWK (exact claims: ``sub`` / ``aud`` / ``iss`` / ``exp`` window / unique
``jti`` / ``sans``), the register-response contract (``ca_ott`` present, null
when the provisioner JWK is absent/malformed — the documented fail-safe), and
the operator re-issue endpoint matrix (admin-gated, pending/active OK, revoked
409, JWK-unset 503, audited by jti — never the token).

Runs against the migrated pgserver Postgres (mirrors test_agents_p5t1's harness).
NO migration is involved: ``ca_ott`` is a response field + env config only.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from authlib.jose import JsonWebKey
from joserfc import jwt as jose_jwt
from joserfc.jwk import ECKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import agentsync
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent

CA_URL = "https://ca.filearr.lan:9000"
PROVISIONER = "filearr-agents"

# A throwaway provisioner keypair for the whole module: the PRIVATE JWK is what
# central signs with (FILEARR_CA_PROVISIONER_JWK); the PUBLIC JWK is what a
# step-ca stand-in (here, the test) verifies the OTT with.
_PRIV = JsonWebKey.generate_key("EC", "P-256", is_private=True).as_dict(is_private=True)
PRIV_JWK_JSON = json.dumps(_PRIV)
_PUB = {k: v for k, v in _PRIV.items() if k != "d"}
PUB_KEY = ECKey.import_key(_PUB)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _verify(token: str):
    """Decode+signature-verify an OTT against the module public key, return claims."""
    return jose_jwt.decode(token, PUB_KEY)


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
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "ca_url", CA_URL)
    monkeypatch.setattr(settings, "ca_fingerprint", "deadbeef")
    monkeypatch.setattr(settings, "ca_provisioner", PROVISIONER)
    monkeypatch.setattr(settings, "ca_provisioner_jwk", PRIV_JWK_JSON)
    monkeypatch.setattr(settings, "ca_ott_ttl_seconds", 300)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _mint_token(c) -> str:
    return (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]


async def _register(c, platform="linux", hostname="h"):
    raw = await _mint_token(c)
    r = await c.post(
        "/api/v1/agents/register",
        json={"token": raw, "hostname": hostname, "platform": platform},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Pure mint: exact claim shape + jti uniqueness                               #
# --------------------------------------------------------------------------- #
def test_mint_ca_ott_claim_shape():
    aid = uuid.uuid4()
    jwk = agentsync.load_provisioner_jwk(PRIV_JWK_JSON)
    before = int(time.time())
    token, jti = agentsync.mint_ca_ott(
        aid, jwk=jwk, ca_url=CA_URL, provisioner=PROVISIONER, ttl_seconds=300
    )
    after = int(time.time())
    decoded = _verify(token)
    claims, header = decoded.claims, decoded.header

    assert claims["sub"] == str(aid)
    assert claims["sans"] == [str(aid)]
    assert claims["iss"] == PROVISIONER
    assert claims["aud"] == f"{CA_URL}/1.0/sign"
    assert claims["jti"] == jti
    # timing window: iat/nbf ~ now, exp = iat + ttl
    assert before <= claims["iat"] <= after
    assert claims["nbf"] == claims["iat"]
    assert claims["exp"] == claims["iat"] + 300
    # header: ES256 + JWT + kid matches the provisioner key (kid or thumbprint)
    assert header["alg"] == "ES256"
    assert header["typ"] == "JWT"
    assert header["kid"] == (jwk.get("kid") or PUB_KEY.thumbprint())


def test_jti_unique_across_mints():
    aid = uuid.uuid4()
    jwk = agentsync.load_provisioner_jwk(PRIV_JWK_JSON)
    jtis = {
        agentsync.mint_ca_ott(
            aid, jwk=jwk, ca_url=CA_URL, provisioner=PROVISIONER, ttl_seconds=300
        )[1]
        for _ in range(25)
    }
    assert len(jtis) == 25  # unique nonce per mint (step-ca replay defence)


def test_load_provisioner_jwk_fail_safe():
    assert agentsync.load_provisioner_jwk(None) is None
    assert agentsync.load_provisioner_jwk("") is None
    assert agentsync.load_provisioner_jwk("not-json{") is None
    # RSA key is not EC P-256 -> rejected (fail-safe null, not a crash)
    rsa = json.dumps({"kty": "RSA", "n": "abc", "e": "AQAB", "d": "xyz"})
    assert agentsync.load_provisioner_jwk(rsa) is None
    # public EC (no private 'd') cannot sign -> rejected
    assert agentsync.load_provisioner_jwk(json.dumps(_PUB)) is None
    # a valid private EC P-256 parses
    assert agentsync.load_provisioner_jwk(PRIV_JWK_JSON) is not None


# --------------------------------------------------------------------------- #
# Register response contract                                                  #
# --------------------------------------------------------------------------- #
async def test_register_returns_verifiable_ca_ott(client):
    c, _, _ = client
    out = await _register(c)
    assert "ca_ott" in out
    token = out["ca_ott"]
    assert token is not None
    claims = _verify(token).claims
    # OTT sub/sans bind the server-assigned agent_id (R3).
    assert claims["sub"] == out["agent_id"]
    assert claims["sans"] == [out["agent_id"]]
    assert claims["aud"] == f"{CA_URL}/1.0/sign"


async def test_register_ca_ott_null_when_jwk_absent(client):
    c, _, settings = client
    import pytest as _pt

    with _pt.MonkeyPatch.context() as m:
        m.setattr(settings, "ca_provisioner_jwk", None)
        out = await _register(c, hostname="nojwk")
    # register still 201 (asserted in _register); ca_ott is null.
    assert out["ca_ott"] is None
    # bootstrap info still present (agent can pin the CA once a key is plumbed).
    assert out["ca"]["url"] == CA_URL


async def test_register_ca_ott_null_when_jwk_malformed(client):
    c, _, settings = client
    import pytest as _pt

    for bad in ("not-json{", json.dumps({"kty": "RSA", "n": "a", "e": "b", "d": "c"})):
        with _pt.MonkeyPatch.context() as m:
            m.setattr(settings, "ca_provisioner_jwk", bad)
            out = await _register(c, hostname="badjwk")
        assert out["ca_ott"] is None  # fail-safe: register succeeds, ca_ott null


async def test_ott_and_token_never_logged_in_audit(client):
    c, maker, _ = client
    out = await _register(c)
    token = out["ca_ott"]
    async with maker() as s:
        rows = (
            await s.execute(text("SELECT event_type, details FROM security_events"))
        ).all()
    types = [r[0] for r in rows]
    assert "agent_ca_ott_minted" in types
    minted = [r[1] for r in rows if r[0] == "agent_ca_ott_minted"]
    assert minted and minted[0]["jti"]
    assert minted[0]["agent_id"] == out["agent_id"]
    # the raw OTT must appear in NO audit detail payload.
    blob = json.dumps([r[1] for r in rows])
    assert token not in blob


# --------------------------------------------------------------------------- #
# Re-issue endpoint matrix                                                     #
# --------------------------------------------------------------------------- #
async def test_reissue_pending_and_active(client):
    c, _, _ = client
    out = await _register(c)
    aid, secret = out["agent_id"], out["enroll_secret"]

    # pending agent: fresh OTT
    r1 = await c.post(f"/api/v1/agents/{aid}/ca-ott")
    assert r1.status_code == 200, r1.text
    t1 = r1.json()["ca_ott"]
    assert _verify(t1).claims["sub"] == aid
    assert r1.json()["ca"]["url"] == CA_URL

    # bind a cert (pending -> active), then re-issue again
    b = await c.post(
        f"/api/v1/agents/{aid}/certificate",
        json={"enroll_secret": secret, "cert_fingerprint": "AA:BB"},
    )
    assert b.status_code == 200
    r2 = await c.post(f"/api/v1/agents/{aid}/ca-ott")
    assert r2.status_code == 200
    # distinct jti from the register-time OTT and the pending re-issue.
    assert _verify(r2.json()["ca_ott"]).claims["jti"] != _verify(t1).claims["jti"]


async def test_reissue_revoked_409(client):
    c, _, _ = client
    aid = (await _register(c))["agent_id"]
    await c.delete(f"/api/v1/agents/{aid}")  # revoke
    r = await c.post(f"/api/v1/agents/{aid}/ca-ott")
    assert r.status_code == 409


async def test_reissue_missing_404(client):
    c, _, _ = client
    r = await c.post(f"/api/v1/agents/{uuid.uuid4()}/ca-ott")
    assert r.status_code == 404


async def test_reissue_503_when_jwk_unset(client):
    c, _, settings = client
    import pytest as _pt

    aid = (await _register(c))["agent_id"]
    with _pt.MonkeyPatch.context() as m:
        m.setattr(settings, "ca_provisioner_jwk", None)
        r = await c.post(f"/api/v1/agents/{aid}/ca-ott")
    assert r.status_code == 503


async def test_reissue_audited_by_jti(client):
    c, maker, _ = client
    aid = (await _register(c))["agent_id"]
    async with maker() as s:  # clear register-time mint event for a clean assert
        await s.execute(text("DELETE FROM security_events"))
        await s.commit()
    r = await c.post(f"/api/v1/agents/{aid}/ca-ott")
    assert r.status_code == 200
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT details FROM security_events WHERE event_type='agent_ca_ott_minted'")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][0]["via"] == "reissue"
    assert rows[0][0]["jti"]


# --------------------------------------------------------------------------- #
# Feature gate + admin gating on the re-issue endpoint                        #
# --------------------------------------------------------------------------- #
async def test_reissue_feature_gate_404_when_disabled(client, monkeypatch):
    c, _, settings = client
    aid = (await _register(c))["agent_id"]
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.post(f"/api/v1/agents/{aid}/ca-ott")).status_code == 404


async def test_reissue_admin_scope_required(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "ca_provisioner_jwk", PRIV_JWK_JSON)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/api/v1/agents/{uuid.uuid4()}/ca-ott")
        assert r.status_code == 401  # no admin bearer
    app.dependency_overrides.clear()
