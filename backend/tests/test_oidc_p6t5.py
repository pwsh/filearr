"""P6-T5 — OIDC SSO (Authlib RP).

A network-free **mock IdP**: a locally-generated RSA key + its JWKS + hand-signed
ID tokens stand in for a real provider, so the full flow (state/nonce/PKCE, code
exchange, ID-token validation, JIT provisioning, role + group sync, session mint)
is exercised without a container or a socket. Tamper cases (bad signature, wrong
aud, expired, replayed state, nonce mismatch, at_hash mismatch — the
CVE-2026-28498 fail-open class) all assert fail-closed.
"""

from __future__ import annotations

import base64
import hashlib
import time
import warnings
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, oidc
from filearr import db as db_mod
from filearr.authx import AuthResult
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Principal, PrincipalGroup, PrincipalGroupMember, User

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

BACKEND_DIR = Path(__file__).resolve().parent.parent
ISSUER = "https://idp.test"
CLIENT_ID = "filearr-client"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Mock IdP: key, JWKS, token minting                                          #
# --------------------------------------------------------------------------- #
_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)
_KID = "mock-kid-1"
_PUB = {**_KEY.as_dict(is_private=False), "kid": _KID}
_JWKS = {"keys": [_PUB]}
_ATTACKER_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)


def _at_hash(access_token: str, alg: str = "RS256") -> str:
    digest = hashlib.sha256(access_token.encode()).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()


def mint_id_token(
    *,
    nonce: str,
    sub: str = "sub-123",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    access_token: str | None = "access-tok",
    at_hash: str | None = "__auto__",
    exp_delta: int = 300,
    extra: dict | None = None,
    key=_KEY,
) -> str:
    now = int(time.time())
    payload = {
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "exp": now + exp_delta,
        "iat": now,
        "nonce": nonce,
    }
    if access_token is not None and at_hash == "__auto__":
        payload["at_hash"] = _at_hash(access_token)
    elif at_hash not in (None, "__auto__"):
        payload["at_hash"] = at_hash
    if extra:
        payload.update(extra)
    jwt = JsonWebToken(["RS256"])
    return jwt.encode({"alg": "RS256", "kid": _KID}, payload, key).decode()


def _config(**overrides) -> oidc.OidcConfig:
    base = dict(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",
        scopes=("openid", "profile", "email"),
        redirect_uri=None,
        role_claim=None,
        role_map={},
        default_role="viewer",
        auto_provision=True,
        username_claim="preferred_username",
        link_by_email=False,
        group_claim=None,
        state_ttl_minutes=10,
        http_timeout_s=5.0,
        discovery_max_bytes=1_000_000,
        metadata_cache_seconds=3600,
    )
    base.update(overrides)
    return oidc.OidcConfig(**base)


def _credentials(id_token: str, *, nonce: str, access_token: str = "access-tok") -> dict:
    return {
        "id_token": id_token,
        "access_token": access_token,
        "nonce": nonce,
        "jwks": _JWKS,
        "signing_algs": ["RS256"],
    }


# --------------------------------------------------------------------------- #
# Pure: ID-token validation + tamper cases                                    #
# --------------------------------------------------------------------------- #
def test_authenticate_valid_returns_authresult():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", extra={"email": "a@b.com", "preferred_username": "alice"})
    res = p.authenticate(_credentials(tok, nonce="n1"))
    assert isinstance(res, AuthResult)
    assert res.external_subject == "sub-123"
    assert res.session_hint["issuer"] == ISSUER
    assert res.session_hint["email"] == "a@b.com"
    assert res.session_hint["role"] == "viewer"  # default


def test_authenticate_bad_signature_rejected():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", key=_ATTACKER_KEY)  # signed by wrong key
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1"))


def test_authenticate_wrong_aud_rejected():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", aud="someone-else")
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1"))


def test_authenticate_wrong_issuer_rejected():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", iss="https://evil.test")
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1"))


def test_authenticate_expired_rejected():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", exp_delta=-1000)
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1"))


def test_authenticate_nonce_mismatch_rejected():
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="the-real-nonce")
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="a-different-nonce"))


def test_authenticate_at_hash_tamper_rejected():
    # CVE-2026-28498 fail-open regression: an ID token whose at_hash does not bind
    # the presented access token MUST be rejected.
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", at_hash=_at_hash("a-totally-different-token"))
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1", access_token="access-tok"))


def test_authenticate_missing_at_hash_with_access_token_rejected():
    # Fail-closed: an access token was issued but the binding claim is absent.
    p = oidc.OIDCProvider(_config())
    tok = mint_id_token(nonce="n1", at_hash=None)
    with pytest.raises(oidc.OIDCError) as ei:
        p.authenticate(_credentials(tok, nonce="n1", access_token="access-tok"))
    assert ei.value.reason == "missing_at_hash"


def test_authenticate_alg_none_rejected():
    # An unsigned ('none') token must never be accepted.
    jwt_none = JsonWebToken(["none"])
    now = int(time.time())
    tok = jwt_none.encode(
        {"alg": "none"},
        {"iss": ISSUER, "sub": "x", "aud": CLIENT_ID, "exp": now + 300, "iat": now, "nonce": "n1"},
        key="",
    ).decode()
    p = oidc.OIDCProvider(_config())
    with pytest.raises(oidc.OIDCError):
        p.authenticate(_credentials(tok, nonce="n1", access_token=""))


# --------------------------------------------------------------------------- #
# Pure: role mapping, group extraction, PKCE, URL safety, config parsing      #
# --------------------------------------------------------------------------- #
def test_role_map_highest_privilege_wins():
    p = oidc.OIDCProvider(
        _config(role_claim="groups", role_map={"admins": "admin", "staff": "user"})
    )
    assert p.resolve_role({"groups": ["staff", "admins"]}) == "admin"
    assert p.resolve_role({"groups": ["staff"]}) == "user"


def test_role_map_default_when_unmapped():
    p = oidc.OIDCProvider(
        _config(role_claim="groups", role_map={"admins": "admin"}, default_role="viewer")
    )
    assert p.resolve_role({"groups": ["nobody"]}) == "viewer"


def test_role_refuse_when_unmapped_and_empty_default():
    # Fail-closed refuse mode: empty default_role + no match => None (refuse).
    p = oidc.OIDCProvider(
        _config(role_claim="groups", role_map={"admins": "admin"}, default_role="")
    )
    assert p.resolve_role({"groups": ["nobody"]}) is None


def test_authenticate_refuses_when_role_none():
    p = oidc.OIDCProvider(
        _config(role_claim="groups", role_map={"admins": "admin"}, default_role="")
    )
    tok = mint_id_token(nonce="n1", extra={"groups": ["nobody"]})
    with pytest.raises(oidc.OIDCError) as ei:
        p.authenticate(_credentials(tok, nonce="n1"))
    assert ei.value.reason == "no_role"


def test_resolve_groups_extracts_claim():
    p = oidc.OIDCProvider(_config(group_claim="groups"))
    assert p.resolve_groups({"groups": ["a", "b"]}) == ("a", "b")
    assert p.resolve_groups({"groups": "solo"}) == ("solo",)
    assert p.resolve_groups({}) == ()
    assert oidc.OIDCProvider(_config()).resolve_groups({"groups": ["a"]}) == ()


def test_pkce_s256_challenge_matches_verifier():
    verifier, challenge = oidc.generate_pkce()
    expect = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expect


def test_require_safe_url():
    oidc._require_safe_url("https://ok.test/x")
    oidc._require_safe_url("http://localhost:8080/x")
    oidc._require_safe_url("http://127.0.0.1/x")
    with pytest.raises(oidc.OIDCError):
        oidc._require_safe_url("http://evil.test/x")


def test_config_parsing_helpers():
    get_settings.cache_clear()
    s = get_settings()
    s.oidc_scopes = "profile email"
    assert "openid" in s.oidc_scope_list
    s.oidc_role_map = "admins:admin, editors:user , bad:notarole,noColon"
    assert s.oidc_role_map_parsed == {"admins": "admin", "editors": "user"}
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# DB fixtures                                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM auth_rate_limits"))
        await conn.execute(text("DELETE FROM oidc_login_states"))
        await conn.execute(text("DELETE FROM principal_group_members"))
        await conn.execute(text("DELETE FROM principal_groups"))
        await conn.execute(text("DELETE FROM sessions"))
        await conn.execute(text("DELETE FROM users"))
        await conn.execute(text("DELETE FROM principals"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def oidc_settings():
    """A settings object with OIDC enabled + configured, restored after."""
    get_settings.cache_clear()
    s = get_settings()
    s.auth_enabled = True
    s.oidc_enabled = True
    s.oidc_issuer = ISSUER
    s.oidc_client_id = CLIENT_ID
    s.oidc_client_secret = "shh"
    s.oidc_scopes = "openid profile email"
    s.oidc_role_claim = None
    s.oidc_role_map = ""
    s.oidc_default_role = "viewer"
    s.oidc_auto_provision = True
    s.oidc_link_by_email = False
    s.oidc_group_claim = None
    yield s
    get_settings.cache_clear()


async def _provision(maker, auth_result):
    async with maker() as s:
        res = await oidc.provision_principal(s, auth_result)
        await s.commit()
        return res


def _authresult(
    sub="sub-123", role="viewer", email="", groups=(), verified=False, username="alice"
):
    return AuthResult(
        external_subject=sub,
        external_groups=tuple(groups),
        session_hint={
            "issuer": ISSUER,
            "role": role,
            "email": email,
            "email_verified": "true" if verified else "false",
            "preferred_username": username,
            "name": "",
        },
    )


# --------------------------------------------------------------------------- #
# Provisioning / linking / role + group sync                                  #
# --------------------------------------------------------------------------- #
async def test_jit_provision_creates_sso_only_user(db_maker, oidc_settings):
    await _provision(db_maker, _authresult(role="user", email="a@b.com", username="alice"))
    async with db_maker() as s:
        u = (
            await s.execute(
                text(
                    "SELECT username, password_hash, auth_provider, "
                    "external_issuer, external_subject FROM users"
                )
            )
        ).one()
    assert u[0] == "alice"
    assert u[1] is None  # SSO-only: NO password hash
    assert u[2] == "oidc"
    assert u[3] == ISSUER and u[4] == "sub-123"
    # And the SSO-only account cannot log in locally (password rejected).
    async with db_maker() as s:
        assert await authx.authenticate_local(s, "alice", "anything") is None


async def test_jit_username_collision_suffix(db_maker, oidc_settings):
    # Seed a local "alice"; the OIDC user must get "alice1".
    async with db_maker() as s:
        p = Principal(kind="user", global_role="viewer")
        s.add(p)
        await s.flush()
        s.add(User(principal_id=p.id, username="alice", password_hash="x", auth_provider="local"))
        await s.commit()
    await _provision(db_maker, _authresult(username="alice"))
    async with db_maker() as s:
        names = sorted(r[0] for r in (await s.execute(text("SELECT username FROM users"))).all())
    assert names == ["alice", "alice1"]


async def test_existing_identity_role_updates_on_next_login(db_maker, oidc_settings):
    r1 = await _provision(db_maker, _authresult(role="viewer"))
    r2 = await _provision(db_maker, _authresult(role="admin"))
    assert r1.principal_id == r2.principal_id  # same identity, no duplicate
    assert r2.role_changed is True
    async with db_maker() as s:
        role = (await s.execute(text("SELECT global_role FROM principals"))).scalar_one()
    assert role == "admin"
    async with db_maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert n == 1


async def test_no_auto_provision_refuses(db_maker, oidc_settings):
    oidc_settings.oidc_auto_provision = False
    with pytest.raises(oidc.OIDCError) as ei:
        await _provision(db_maker, _authresult())
    assert ei.value.reason == "no_account"


async def test_link_by_email_default_off_creates_new_account(db_maker, oidc_settings):
    # A local account with the same email must NOT be hijacked when linking is off.
    async with db_maker() as s:
        p = Principal(kind="user", global_role="admin")
        s.add(p)
        await s.flush()
        s.add(User(principal_id=p.id, username="bob", email="bob@b.com",
                   password_hash="x", auth_provider="local"))
        await s.commit()
    await _provision(
        db_maker,
        _authresult(sub="sub-bob", email="bob@b.com", verified=True, username="bob"),
    )
    async with db_maker() as s:
        rows = (
            await s.execute(
                text("SELECT username, auth_provider FROM users ORDER BY username")
            )
        ).all()
    # Two distinct accounts — the local 'bob' untouched, a new 'bob1' SSO account.
    assert ("bob", "local") in rows
    assert any(prov == "oidc" for _, prov in rows)


async def test_link_by_email_on_links_verified_match(db_maker, oidc_settings):
    oidc_settings.oidc_link_by_email = True
    async with db_maker() as s:
        p = Principal(kind="user", global_role="user")
        s.add(p)
        await s.flush()
        s.add(User(principal_id=p.id, username="carol", email="carol@b.com",
                   password_hash="x", auth_provider="local"))
        await s.commit()
    await _provision(
        db_maker,
        _authresult(sub="sub-carol", email="carol@b.com", verified=True, username="carol"),
    )
    async with db_maker() as s:
        rows = (
            await s.execute(
                text("SELECT username, auth_provider, external_subject FROM users")
            )
        ).all()
    assert len(rows) == 1  # linked, not duplicated
    assert rows[0][1] == "oidc" and rows[0][2] == "sub-carol"


async def test_group_sync_adds_and_removes_oidc_groups(db_maker, oidc_settings):
    async with db_maker() as s:
        g_oidc = PrincipalGroup(name="editors", source="oidc")
        g_local = PrincipalGroup(name="localteam", source="local")
        s.add_all([g_oidc, g_local])
        await s.commit()
        gid_oidc, gid_local = g_oidc.id, g_local.id
    # First login: member of both editors (idp) + a manual local group.
    res = await _provision(db_maker, _authresult(groups=("editors",)))
    pid = res.principal_id
    async with db_maker() as s:
        s.add(PrincipalGroupMember(principal_id=pid, group_id=gid_local))
        await s.commit()
    async with db_maker() as s:
        rows = (
            await s.execute(text("SELECT group_id FROM principal_group_members"))
        ).all()
        members = {r[0] for r in rows}
    assert gid_oidc in members and gid_local in members
    # Second login WITHOUT editors: the source='oidc' group is removed, the manual
    # source='local' membership is preserved.
    await _provision(db_maker, _authresult(groups=()))
    async with db_maker() as s:
        rows = (
            await s.execute(text("SELECT group_id FROM principal_group_members"))
        ).all()
        members = {r[0] for r in rows}
    assert gid_oidc not in members and gid_local in members


async def test_disabled_principal_refused(db_maker, oidc_settings):
    await _provision(db_maker, _authresult())
    async with db_maker() as s:
        await s.execute(text("UPDATE principals SET disabled_at = now()"))
        await s.commit()
    with pytest.raises(oidc.OIDCError) as ei:
        await _provision(db_maker, _authresult())
    assert ei.value.reason == "disabled"


# --------------------------------------------------------------------------- #
# Endpoint flow (httpx ASGI, network monkeypatched)                           #
# --------------------------------------------------------------------------- #
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture
async def client(db_maker, oidc_settings, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    oidc.clear_caches()

    async def _fake_metadata(cfg):
        return DISCOVERY

    async def _fake_jwks(cfg, uri):
        return _JWKS

    monkeypatch.setattr(oidc, "fetch_metadata", _fake_metadata)
    monkeypatch.setattr(oidc, "fetch_jwks", _fake_jwks)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, monkeypatch
    app.dependency_overrides.clear()


async def test_status_reports_oidc_enabled(client):
    c, _ = client
    r = await c.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["oidc_enabled"] is True


async def test_login_redirects_to_idp_with_pkce(client, db_maker):
    c, _ = client
    r = await c.get("/api/v1/auth/oidc/login")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(f"{ISSUER}/authorize?")
    q = parse_qs(urlsplit(loc).query)
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == [CLIENT_ID]
    assert "state" in q and "nonce" in q and "code_challenge" in q
    # A single-use login-state row was persisted.
    async with db_maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM oidc_login_states"))).scalar_one()
    assert n == 1


async def _start_login(c):
    r = await c.get("/api/v1/auth/oidc/login")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


async def test_callback_full_flow_mints_session(client, db_maker):
    c, monkeypatch = client
    state, nonce = await _start_login(c)
    id_token = mint_id_token(nonce=nonce, extra={"preferred_username": "eve", "email": "eve@b.com"})

    async def _fake_exchange(meta, cfg, *, code, redirect_uri, code_verifier):
        return {"id_token": id_token, "access_token": "access-tok"}

    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange)
    r = await c.get(f"/api/v1/auth/oidc/callback?code=abc&state={state}")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    setc = r.headers.get("set-cookie", "").lower()
    assert "filearr_session" in setc and "httponly" in setc and "samesite=lax" in setc
    # A user + a session were created.
    async with db_maker() as s:
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM sessions"))).scalar_one() == 1
    # The minted cookie is a real, working session (/auth/me resolves it).
    me = await c.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "eve"


async def test_callback_replayed_state_rejected(client):
    c, monkeypatch = client
    state, nonce = await _start_login(c)
    id_token = mint_id_token(nonce=nonce)

    async def _fake_exchange(meta, cfg, *, code, redirect_uri, code_verifier):
        return {"id_token": id_token, "access_token": "access-tok"}

    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange)
    r1 = await c.get(f"/api/v1/auth/oidc/callback?code=abc&state={state}")
    assert r1.status_code == 303 and "sso_error" not in r1.headers["location"]
    # Replaying the now-consumed state must fail closed.
    r2 = await c.get(f"/api/v1/auth/oidc/callback?code=abc&state={state}")
    assert r2.status_code == 303
    assert "sso_error=bad_state" in r2.headers["location"]


async def test_callback_idp_error_param_redirects(client):
    c, _ = client
    r = await c.get("/api/v1/auth/oidc/callback?error=access_denied")
    assert r.status_code == 303
    assert "sso_error=access_denied" in r.headers["location"]


async def test_endpoints_404_when_disabled(client):
    c, monkeypatch = client
    get_settings.cache_clear()
    s = get_settings()
    s.oidc_enabled = False
    r = await c.get("/api/v1/auth/oidc/login")
    assert r.status_code == 404
    st = await c.get("/api/v1/auth/status")
    assert st.json()["oidc_enabled"] is False
    s.oidc_enabled = True


async def test_status_oidc_false_when_auth_off(client):
    c, _ = client
    get_settings.cache_clear()
    s = get_settings()
    s.auth_enabled = False
    st = await c.get("/api/v1/auth/status")
    assert st.json()["oidc_enabled"] is False  # auth off => fail closed
    s.auth_enabled = True
