"""P6-T1 — local accounts + Postgres-backed cookie sessions + the auth gate.

Four layers:
* pure argon2 password hashing (hash/verify/wrong/rehash) — no DB;
* pure role→scope mapping;
* session lifecycle against real Postgres (create/validate/rotate/expiry/
  inactivity/revoke) + proof the RAW token is never stored (only its sha256);
* endpoint flows with real Postgres + httpx: bootstrap once-only, login/logout/
  me, cookie flags (HttpOnly/SameSite/Secure-under-forwarded-proto), user CRUD +
  role→scope enforcement, and — the load-bearing compatibility guarantee — the
  AUTH-OFF regression (a protected endpoint behaves byte-for-byte as today) and
  API-key coexistence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import ApiKey, Principal, User
from filearr.security import generate_key

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure: argon2 password hashing                                               #
# --------------------------------------------------------------------------- #
def test_hash_is_argon2id_and_verifies():
    h = authx.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")  # Argon2id, not bcrypt/sha
    assert authx.verify_password("correct horse battery staple", h) is True


def test_verify_rejects_wrong_password():
    h = authx.hash_password("s3cret-pw")
    assert authx.verify_password("wrong-pw", h) is False


def test_verify_none_hash_fails_closed():
    # A federated-only account (NULL password_hash) can never log in locally.
    assert authx.verify_password("anything", None) is False


def test_hash_is_salted_unique():
    assert authx.hash_password("pw") != authx.hash_password("pw")


def test_needs_rehash_false_for_current_params():
    assert authx.needs_rehash(authx.hash_password("pw")) is False


def test_password_never_stored_plaintext():
    h = authx.hash_password("plaintextpw")
    assert "plaintextpw" not in h


# --------------------------------------------------------------------------- #
# Pure: role → scope mapping                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "role,scopes",
    [
        ("admin", {"admin", "write", "read"}),
        ("user", {"write", "read"}),
        ("viewer", {"read"}),
        ("bogus", set()),
    ],
)
def test_role_scope_mapping(role, scopes):
    assert set(authx.scopes_for_role(role)) == scopes


def test_session_token_hash_is_sha256_hex():
    tok = authx.mint_session_token()
    assert len(tok.session_hash) == 64
    assert authx.hash_session_token(tok.raw) == tok.session_hash
    assert tok.raw not in tok.session_hash  # raw not recoverable from the hash


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
        await conn.execute(text("DELETE FROM sessions"))
        await conn.execute(text("DELETE FROM users"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM principals"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.app_transport = transport  # exposed for tests needing a 2nd cookie jar
        yield c, settings
    app.dependency_overrides.clear()


async def _seed_user(maker, username, password, role="user", disabled=False):
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        u = User(
            principal_id=p.id,
            username=username.lower(),
            password_hash=authx.hash_password(password),
            auth_provider="local",
        )
        if disabled:
            from datetime import UTC as _U
            from datetime import datetime as _D

            p.disabled_at = _D.now(_U)
        s.add(u)
        await s.commit()
        return str(p.id)


# --------------------------------------------------------------------------- #
# Session lifecycle (direct authx calls, real Postgres)                       #
# --------------------------------------------------------------------------- #
async def test_session_create_validate_and_sha256_at_rest(db_maker):
    pid = await _seed_user(db_maker, "alice", "pw-alice-123")
    async with db_maker() as s:
        tok = await authx.create_session(s, pid, ip_address="10.0.0.9", user_agent="pytest")
        await s.commit()
    # The RAW token must NOT be in the DB — only its sha256.
    async with db_maker() as s:
        rows = (await s.execute(text("SELECT session_hash, ip_address FROM sessions"))).all()
    assert len(rows) == 1
    assert rows[0][0] == authx.hash_session_token(tok.raw)
    assert tok.raw != rows[0][0]
    assert str(rows[0][1]) == "10.0.0.9"
    # Validate the presented token resolves the principal.
    async with db_maker() as s:
        v = await authx.validate_session(s, tok.raw)
        await s.commit()
    assert v is not None and v.principal_id == pid


async def test_session_absolute_expiry(db_maker):
    pid = await _seed_user(db_maker, "bob", "pw-bob-1234")
    async with db_maker() as s:
        tok = await authx.create_session(s, pid)
        await s.commit()
    # Fast-forward: push expires_absolute into the past.
    async with db_maker() as s:
        await s.execute(
            text("UPDATE sessions SET expires_absolute = :t"),
            {"t": datetime.now(UTC) - timedelta(hours=1)},
        )
        await s.commit()
    async with db_maker() as s:
        v = await authx.validate_session(s, tok.raw)
        await s.commit()
    assert v is None
    async with db_maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
    assert n == 0  # expired row is reaped on validate


async def test_session_inactivity_expiry(db_maker):
    pid = await _seed_user(db_maker, "carol", "pw-carol-123")
    async with db_maker() as s:
        tok = await authx.create_session(s, pid)
        await s.commit()
    async with db_maker() as s:
        await s.execute(
            text("UPDATE sessions SET last_seen_at = :t"),
            {"t": datetime.now(UTC) - timedelta(days=8)},  # > 7d idle
        )
        await s.commit()
    async with db_maker() as s:
        v = await authx.validate_session(s, tok.raw)
        await s.commit()
    assert v is None


async def test_session_rotation_reissues_token(db_maker):
    pid = await _seed_user(db_maker, "dave", "pw-dave-1234")
    async with db_maker() as s:
        tok = await authx.create_session(s, pid)
        await s.commit()
    async with db_maker() as s:
        await s.execute(
            text("UPDATE sessions SET rotated_at = :t"),
            {"t": datetime.now(UTC) - timedelta(minutes=11)},  # > 10min
        )
        await s.commit()
    async with db_maker() as s:
        v = await authx.validate_session(s, tok.raw)
        await s.commit()
    assert v is not None
    assert v.rotated is not None
    assert v.rotated.raw != tok.raw
    # Old token no longer resolves; new one does.
    async with db_maker() as s:
        assert await authx.validate_session(s, tok.raw) is None
    async with db_maker() as s:
        v2 = await authx.validate_session(s, v.rotated.raw)
        await s.commit()
    assert v2 is not None and v2.principal_id == pid


async def test_session_revoke_is_immediate(db_maker):
    pid = await _seed_user(db_maker, "erin", "pw-erin-1234")
    async with db_maker() as s:
        tok = await authx.create_session(s, pid)
        await s.commit()
    async with db_maker() as s:
        assert await authx.revoke_session(s, tok.raw) is True
        await s.commit()
    async with db_maker() as s:
        assert await authx.validate_session(s, tok.raw) is None


async def test_disabled_principal_authenticate_local_fails(db_maker):
    await _seed_user(db_maker, "frank", "pw-frank-123", disabled=True)
    async with db_maker() as s:
        assert await authx.authenticate_local(s, "frank", "pw-frank-123") is None


# --------------------------------------------------------------------------- #
# Endpoint: bootstrap / login / me / logout                                   #
# --------------------------------------------------------------------------- #
async def test_status_bootstrap_mode_then_enabled(client):
    c, _ = client
    r = await c.get("/api/v1/auth/status")
    assert r.status_code == 200
    # P6-T5 added oidc_enabled; P6-T6 added ldap_enabled (both false here).
    assert r.json() == {
        "auth_enabled": True,
        "users_exist": False,
        "mode": "bootstrap",
        "oidc_enabled": False,
        "ldap_enabled": False,
    }
    r = await c.post(
        "/api/v1/auth/bootstrap", json={"username": "Admin", "password": "admin-pw-123"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["global_role"] == "admin"
    assert r.json()["username"] == "admin"  # normalized lowercase
    r = await c.get("/api/v1/auth/status")
    assert r.json()["mode"] == "enabled"


async def test_bootstrap_is_once_only(client):
    c, _ = client
    r1 = await c.post(
        "/api/v1/auth/bootstrap", json={"username": "first", "password": "first-pw-123"}
    )
    assert r1.status_code == 201
    r2 = await c.post(
        "/api/v1/auth/bootstrap", json={"username": "second", "password": "second-pw-1"}
    )
    assert r2.status_code == 409


async def test_login_sets_cookie_and_me_roundtrip(client):
    c, settings = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    r = await c.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw-123"}
    )
    assert r.status_code == 200, r.text
    assert settings.session_cookie_name in r.cookies
    me = await c.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    # logout invalidates the cookie on the very next request.
    lo = await c.post("/api/v1/auth/logout")
    assert lo.status_code == 204
    me2 = await c.get("/api/v1/auth/me")
    assert me2.status_code == 401


async def test_login_wrong_password_401(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    r = await c.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "nope"}
    )
    assert r.status_code == 401


async def test_login_cookie_flags_httponly_samesite_and_no_secure_on_http(client):
    c, settings = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    r = await c.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw-123"}
    )
    setc = r.headers["set-cookie"].lower()
    assert "httponly" in setc
    # P6-T5 ruling: the session-cookie SameSite default moved strict→lax so an
    # OIDC callback's cross-site 302→/ return carries the freshly-minted cookie.
    # Lax still withholds the cookie on cross-site POST/PATCH/DELETE (CSRF-safe).
    assert "samesite=lax" in setc
    assert "secure" not in setc  # plain http → no Secure flag
    assert r.json()["warning"] and "plain http" in r.json()["warning"].lower()


async def test_login_cookie_secure_under_forwarded_proto_https(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    r = await c.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin-pw-123"},
        headers={"X-Forwarded-Proto": "https"},
    )
    setc = r.headers["set-cookie"].lower()
    assert "secure" in setc
    assert r.json()["warning"] is None  # https → no plain-http nudge


# --------------------------------------------------------------------------- #
# Endpoint: user CRUD + role→scope enforcement via the shared gate            #
# --------------------------------------------------------------------------- #
async def _login(c, username, password, **headers):
    r = await c.post(
        "/api/v1/auth/login", json={"username": username, "password": password}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r


async def test_admin_creates_users_and_viewer_is_read_only(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    await _login(c, "admin", "admin-pw-123")
    # admin creates a viewer
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "vic", "password": "vic-pw-1234", "global_role": "viewer"},
    )
    assert r.status_code == 201, r.text
    # list users (admin scope)
    r = await c.get("/api/v1/auth/users")
    assert r.status_code == 200
    assert {u["username"] for u in r.json()} == {"admin", "vic"}
    # admin (session) can hit a write endpoint; libraries list is read.
    r = await c.get("/api/v1/libraries")
    assert r.status_code == 200
    # Now log in as the viewer: read works, admin-scope user list is 403.
    await c.post("/api/v1/auth/logout")
    await _login(c, "vic", "vic-pw-1234")
    assert (await c.get("/api/v1/libraries")).status_code == 200
    r = await c.get("/api/v1/auth/users")
    assert r.status_code == 403  # viewer lacks admin scope


async def test_role_change_revokes_sessions(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    await _login(c, "admin", "admin-pw-123")
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "mo", "password": "mo-pw-12345", "global_role": "user"},
    )
    mo_id = r.json()["id"]
    # mo logs in on a second client-session (separate cookie jar).
    transport = c.app_transport
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c2:
        await _login(c2, "mo", "mo-pw-12345")
        assert (await c2.get("/api/v1/auth/me")).status_code == 200
        # admin promotes mo → role change kills mo's sessions.
        r = await c.patch(f"/api/v1/auth/users/{mo_id}", json={"global_role": "admin"})
        assert r.status_code == 200
        assert (await c2.get("/api/v1/auth/me")).status_code == 401


async def test_cannot_delete_last_admin(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    r = await _login(c, "admin", "admin-pw-123")
    me = (await c.get("/api/v1/auth/me")).json()
    r = await c.delete(f"/api/v1/auth/users/{me['id']}")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# AUTH-OFF regression + API-key coexistence                                   #
# --------------------------------------------------------------------------- #
async def test_auth_off_protected_endpoint_open(client):
    """The load-bearing compatibility guarantee: with FILEARR_AUTH_ENABLED=false
    a scope-protected endpoint is reachable with NO credentials — byte-for-byte
    today's behaviour."""
    c, settings = client
    settings.auth_enabled = False
    r = await c.get("/api/v1/libraries")  # require_scope("read")
    assert r.status_code == 200
    # A write endpoint dependency is equally a no-op.
    r = await c.get("/api/v1/auth/users")  # require_scope("admin") — no-op when off
    assert r.status_code == 200


async def test_auth_on_no_credentials_401(client):
    c, _ = client
    # auth enabled (fixture default), no bearer, no cookie.
    r = await c.get("/api/v1/libraries")
    assert r.status_code == 401
    assert "bearer" in r.text.lower()


async def test_api_key_still_works_when_auth_enabled(client, db_maker):
    c, _ = client
    full, prefix, key_hash = generate_key()
    async with db_maker() as s:
        s.add(ApiKey(name="ci", prefix=prefix, key_hash=key_hash, scopes=["read"]))
        await s.commit()
    r = await c.get("/api/v1/libraries", headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 200
    # A read key cannot reach an admin endpoint.
    r = await c.get("/api/v1/auth/users", headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 403


async def test_self_password_change_and_relogin(client):
    c, _ = client
    await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    await _login(c, "admin", "admin-pw-123")
    r = await c.post(
        "/api/v1/auth/password",
        json={"current_password": "admin-pw-123", "new_password": "new-admin-pw-9"},
    )
    assert r.status_code == 204
    # Old session was revoked by the password change.
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    # New password works.
    await _login(c, "admin", "new-admin-pw-9")
    assert (await c.get("/api/v1/auth/me")).status_code == 200
