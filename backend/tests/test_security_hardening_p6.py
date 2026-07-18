"""P6-T8/T9/T11 — brute-force rate limiting, security audit log, session mgmt.

Real Postgres (pgserver) + httpx ASGI. Covers:
* rate limiting: per-username lockout threshold, distributed-brute-force caught
  by the username bucket (many IPs), success clears the username bucket,
  anti-enumeration (unknown vs wrong-password are byte-identical), limiter
  disabled is a no-op;
* audit: the lifecycle/login/grant hooks each write a row, secrets never land in
  a row, and FILEARR_AUDIT_READS gates the 'search' event;
* sessions: own-list + current flag, immediate death on revoke, ownership 404,
  revoke-all, admin force-revoke.
"""

from __future__ import annotations

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

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


_WIPE = [
    "security_events",
    "auth_rate_limits",
    "path_grants",
    "principal_group_members",
    "principal_groups",
    "sessions",
    "users",
    "libraries",
    "principals",
]


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in _WIPE:
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_ratelimit_enabled", True)
    monkeypatch.setattr(settings, "auth_ratelimit_max_attempts", 3)
    monkeypatch.setattr(settings, "auth_ratelimit_window_seconds", 120)
    monkeypatch.setattr(settings, "auth_ratelimit_lock_seconds", 300)
    monkeypatch.setattr(settings, "auth_ratelimit_trust_forwarded_for", False)
    monkeypatch.setattr(settings, "audit_reads", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.app_transport = transport
        yield c, settings, maker
    app.dependency_overrides.clear()


async def _count(maker, table, **where) -> int:
    clause = ""
    if where:
        clause = " WHERE " + " AND ".join(f"{k} = :{k}" for k in where)
    async with maker() as s:
        r = await s.execute(text(f"SELECT count(*) FROM {table}{clause}"), where)
        return int(r.scalar_one())


async def _bootstrap_admin(c, username="admin", password="adminpass1"):
    r = await c.post("/api/v1/auth/bootstrap", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    r = await c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


# --------------------------------------------------------------------------- #
# Rate limiting (P6-T8)                                                        #
# --------------------------------------------------------------------------- #
async def test_username_lockout_threshold(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    # 3 wrong passwords for the same username → all 401, and the 3rd trips a lock.
    for _ in range(3):
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    # 4th (even with the RIGHT password) is refused with 429 + Retry-After.
    r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass1"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


async def test_distributed_brute_force_caught_by_username_bucket(client):
    c, settings, maker = client
    monkeypatch_ip = settings
    monkeypatch_ip.auth_ratelimit_trust_forwarded_for = True
    await _bootstrap_admin(c)
    # Each attempt comes from a DIFFERENT source IP — no per-IP bucket ever trips,
    # but the shared username bucket accumulates and locks.
    for i in range(3):
        r = await c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "nope"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert r.status_code == 401
    r = await c.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminpass1"},
        headers={"X-Forwarded-For": "203.0.113.250"},
    )
    assert r.status_code == 429


async def test_success_clears_username_bucket(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    for _ in range(2):  # below the 3-attempt threshold
        await c.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert await _count(maker, "auth_rate_limits", bucket_kind="username", bucket_key="admin") == 1
    r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass1"})
    assert r.status_code == 200
    # The username bucket is cleared on success (the IP bucket may remain).
    assert await _count(maker, "auth_rate_limits", bucket_kind="username", bucket_key="admin") == 0


async def test_anti_enumeration_unknown_vs_wrong_password_identical(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    # Fresh cookie jar so the bootstrap session does not interfere.
    unknown = await c.post("/api/v1/auth/login", json={"username": "ghost", "password": "whatever"})
    wrong = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "wrongpass"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()  # byte-identical body (no enumeration)
    # Both wrote a login_failure with a NULL principal (unknown/undisclosed).
    assert await _count(maker, "security_events", event_type="login_failure") == 2


async def test_limiter_disabled_is_noop(client):
    c, settings, maker = client
    monkeypatch = settings
    monkeypatch.auth_ratelimit_enabled = False
    await _bootstrap_admin(c)
    for _ in range(6):
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401  # never 429
    assert await _count(maker, "auth_rate_limits") == 0


# --------------------------------------------------------------------------- #
# Audit log (P6-T9)                                                            #
# --------------------------------------------------------------------------- #
async def test_login_success_and_logout_audited(client):
    c, settings, maker = client
    await _bootstrap_admin(c)  # bootstrap + login_success
    await c.post("/api/v1/auth/logout")
    assert await _count(maker, "security_events", event_type="bootstrap") == 1
    assert await _count(maker, "security_events", event_type="login_success") == 1
    assert await _count(maker, "security_events", event_type="logout") == 1


async def test_lockout_event_audited_once(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    for _ in range(4):
        await c.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    # The lock trips exactly once even though further attempts are blocked.
    assert await _count(maker, "security_events", event_type="lockout") == 1


async def test_account_lifecycle_and_grant_events_audited(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    # create user
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "mo", "password": "mopass123", "global_role": "user"},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # role change + disable + enable
    await c.patch(f"/api/v1/auth/users/{pid}", json={"global_role": "viewer"})
    await c.patch(f"/api/v1/auth/users/{pid}", json={"disabled": True})
    await c.patch(f"/api/v1/auth/users/{pid}", json={"disabled": False})
    # group + membership + grant
    g = await c.post("/api/v1/rbac/groups", json={"name": "team", "description": None})
    gid = g.json()["id"]
    await c.post(f"/api/v1/rbac/groups/{gid}/members", json={"principal_id": pid})
    lib = await c.post(
        "/api/v1/libraries",
        json={"name": "L", "root_path": "/data/l"},
    )
    assert lib.status_code in (200, 201), lib.text
    lid = lib.json()["id"]
    gr = await c.post(
        "/api/v1/rbac/grants",
        json={
            "subject_kind": "group",
            "subject_id": gid,
            "library_id": lid,
            "rel_path": "sub",
            "action": "search_metadata",
            "effect": "allow",
        },
    )
    assert gr.status_code == 201, gr.text
    grant_id = gr.json()["id"]
    await c.request("DELETE", f"/api/v1/rbac/grants/{grant_id}")
    await c.post(
        "/api/v1/auth/password",
        json={"current_password": "adminpass1", "new_password": "adminpass2"},
    )
    for et in (
        "user_created",
        "role_changed",
        "user_disabled",
        "user_enabled",
        "group_created",
        "group_membership_changed",
        "grant_created",
        "grant_deleted",
        "password_change",
    ):
        assert await _count(maker, "security_events", event_type=et) >= 1, et


async def test_secrets_never_stored_in_audit(client):
    c, settings, maker = client
    await _bootstrap_admin(c, password="sup3r-s3cret-pw")
    await c.patch(
        "/api/v1/auth/password",
        json={"current_password": "sup3r-s3cret-pw", "new_password": "an0ther-s3cret"},
    )
    async with maker() as s:
        rows = (await s.execute(text("SELECT details::text FROM security_events"))).scalars().all()
    blob = " ".join(r or "" for r in rows)
    assert "sup3r-s3cret-pw" not in blob
    assert "an0ther-s3cret" not in blob


async def test_audit_reads_flag_gates_search_event(client, monkeypatch):
    c, settings, maker = client
    await _bootstrap_admin(c)

    # Stub the Meili client so /search runs without a live Meilisearch.
    class _Res:
        hits: list = []
        estimated_total_hits = 0
        facet_distribution: dict = {}
        facet_stats: dict = {}

    class _Idx:
        async def search(self, *a, **k):
            return _Res()

    class _C:
        def index(self, name):
            return _Idx()

    class _Ctx:
        async def __aenter__(self):
            return _C()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("filearr.api.search.client", lambda: _Ctx())

    # audit_reads OFF → no 'search' row.
    r = await c.get("/api/v1/search?q=hello")
    assert r.status_code == 200, r.text
    assert await _count(maker, "security_events", event_type="search") == 0
    # audit_reads ON → one 'search' row.
    settings.audit_reads = True
    r = await c.get("/api/v1/search?q=hello")
    assert r.status_code == 200
    assert await _count(maker, "security_events", event_type="search") == 1


async def test_audit_feed_admin_only_and_filterable(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    r = await c.get("/api/v1/audit?event_type=bootstrap")
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(e["event_type"] == "bootstrap" for e in body["events"])
    assert len(body["events"]) == 1


# --------------------------------------------------------------------------- #
# Session management (P6-T11)                                                  #
# --------------------------------------------------------------------------- #
async def test_session_list_flags_current(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    r = await c.get("/api/v1/auth/sessions")
    assert r.status_code == 200, r.text
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["current"] is True


async def test_revoke_session_kills_it_next_request(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    # A SECOND independent session for the same admin (fresh cookie jar).
    transport = c.app_transport
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c2:
        lr = await c2.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "adminpass1"},
        )
        assert lr.status_code == 200
        # admin (session 1) lists sessions → 2, finds the non-current one.
        rows = (await c.get("/api/v1/auth/sessions")).json()
        assert len(rows) == 2
        other = next(s for s in rows if not s["current"])
        d = await c.delete(f"/api/v1/auth/sessions/{other['id']}")
        assert d.status_code == 204
        # session 2 is dead on its very next request.
        me2 = await c2.get("/api/v1/auth/me")
        assert me2.status_code == 401


async def test_revoke_session_ownership_404(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    # mo's session id must not be revocable by... mo trying admin's, etc.
    await c.post(
        "/api/v1/auth/users",
        json={"username": "mo", "password": "mopass123", "global_role": "user"},
    )
    transport = c.app_transport
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cmo:
        await cmo.post("/api/v1/auth/login", json={"username": "mo", "password": "mopass123"})
        admin_sessions = (await c.get("/api/v1/auth/sessions")).json()
        admin_sid = admin_sessions[0]["id"]
        # mo tries to revoke ADMIN's session → 404 (never leak another's session).
        d = await cmo.delete(f"/api/v1/auth/sessions/{admin_sid}")
        assert d.status_code == 404


async def test_revoke_all_kills_only_own(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    await c.post(
        "/api/v1/auth/users",
        json={"username": "mo", "password": "mopass123", "global_role": "user"},
    )
    transport = c.app_transport
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cmo:
        await cmo.post("/api/v1/auth/login", json={"username": "mo", "password": "mopass123"})
        # admin logs out everywhere.
        ra = await c.post("/api/v1/auth/sessions/revoke-all")
        assert ra.status_code == 204
        # admin's own next request is now unauth...
        assert (await c.get("/api/v1/auth/me")).status_code == 401
        # ...but mo is untouched.
        assert (await cmo.get("/api/v1/auth/me")).status_code == 200


async def test_admin_force_revoke_user_sessions(client):
    c, settings, maker = client
    await _bootstrap_admin(c)
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "mo", "password": "mopass123", "global_role": "user"},
    )
    pid = r.json()["id"]
    transport = c.app_transport
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cmo:
        await cmo.post("/api/v1/auth/login", json={"username": "mo", "password": "mopass123"})
        # admin can enumerate mo's sessions...
        lst = await c.get(f"/api/v1/auth/users/{pid}/sessions")
        assert lst.status_code == 200
        assert len(lst.json()) == 1
        # ...and force-revoke them.
        d = await c.delete(f"/api/v1/auth/users/{pid}/sessions")
        assert d.status_code == 204
        assert (await cmo.get("/api/v1/auth/me")).status_code == 401
    assert await _count(maker, "security_events", event_type="session_revoked") >= 1
