"""P6-T6 — LDAP / AD bind auth (ldap3).

Network-free: ldap3's offline ``MOCK_SYNC`` strategy stands in for a real
directory. A shared in-memory DIT + an injected connector exercise the full flow
(search-then-bind, direct-bind, attribute + group reads, JIT provisioning, role
map, group sync, session mint) plus the security matrix: LDAP-injection attempts,
empty-password rejection (before any bind), StartTLS-required enforcement,
anonymous-bind rejection, subject-attribute preference, and the grant-cache bump.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from ldap3 import ANONYMOUS, MOCK_SYNC, SIMPLE, Connection, Server
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import ldap_auth
from filearr.config import Settings, get_settings
from filearr.db import get_session
from filearr.ldap_auth import LdapConfig, LDAPError
from filearr.main import create_app
from filearr.models import Principal, PrincipalGroup, PrincipalGroupMember, User

BACKEND_DIR = Path(__file__).resolve().parent.parent

ADMIN_DN = "cn=svc,dc=ex,dc=com"
ALICE_DN = "uid=alice,ou=people,dc=ex,dc=com"
BOB_DN = "uid=bob,ou=people,dc=ex,dc=com"
GRP_ADMINS = "cn=admins,ou=groups,dc=ex,dc=com"
GRP_STAFF = "cn=staff,ou=groups,dc=ex,dc=com"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Fake DIT + injected connector                                               #
# --------------------------------------------------------------------------- #
def _default_dit() -> dict:
    return {
        ADMIN_DN: {"userPassword": "svcpw", "objectClass": ["person"], "cn": "svc"},
        ALICE_DN: {
            "userPassword": "alicepw",
            "uid": "alice",
            "mail": "alice@ex.com",
            "entryUUID": "aaaaaaaa-1111-2222-3333-444444444444",
            "objectClass": ["inetOrgPerson"],
            "memberOf": [GRP_ADMINS],
        },
        BOB_DN: {
            "userPassword": "bobpw",
            "uid": "bob",
            "mail": "bob@ex.com",
            "entryUUID": "bbbbbbbb-1111-2222-3333-444444444444",
            "objectClass": ["inetOrgPerson"],
        },
        GRP_ADMINS: {"objectClass": ["groupOfNames"], "cn": "admins", "member": [ALICE_DN]},
        GRP_STAFF: {"objectClass": ["groupOfNames"], "cn": "staff", "member": [BOB_DN]},
    }


def make_connector(dit: dict | None = None, *, calls: list | None = None):
    """A drop-in for ``ldap_auth.connect`` backed by an offline MOCK_SYNC server
    loaded from ``dit``. Records every (user, password) bind attempt in ``calls``
    (used to prove the empty-password path never touches the server)."""
    dit = dit if dit is not None else _default_dit()

    def connector(cfg, *, user, password):
        if calls is not None:
            calls.append((user, password))
        srv = Server("fake", get_info="ALL")
        auth = SIMPLE if user else ANONYMOUS
        conn = Connection(
            srv,
            user=user or None,
            password=password or None,
            authentication=auth,
            client_strategy=MOCK_SYNC,
        )
        for dn, attrs in dit.items():
            conn.strategy.add_entry(dn, attrs)
        if not conn.bind():
            return None
        return conn

    return connector


def _settings(**over) -> Settings:
    base = dict(
        auth_enabled=True,
        ldap_enabled=True,
        ldap_server="ldap://localhost",
        ldap_bind_dn=ADMIN_DN,
        ldap_bind_password="svcpw",
        ldap_user_base="ou=people,dc=ex,dc=com",
        ldap_user_filter="(uid={username})",
        ldap_attr_username="uid",
        ldap_attr_email="mail",
        ldap_attr_uid="entryUUID",
        ldap_group_base="ou=groups,dc=ex,dc=com",
        ldap_group_filter="(member={user_dn})",
        ldap_role_map=f"{GRP_ADMINS}=>admin;{GRP_STAFF}=>user",
        ldap_default_role="",
        ldap_group_sync=True,
    )
    base.update(over)
    return Settings(**base)


def _cfg(**over) -> LdapConfig:
    return LdapConfig.from_settings(_settings(**over))


# --------------------------------------------------------------------------- #
# Pure: identity resolution (search-then-bind + direct-bind)                   #
# --------------------------------------------------------------------------- #
def test_search_then_bind_success():
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "alice", "alicepw", connector=make_connector()
    )
    assert ident is not None
    assert ident.subject == "aaaaaaaa-1111-2222-3333-444444444444"  # entryUUID, not DN
    assert ident.username == "alice"
    assert ident.email == "alice@ex.com"
    assert GRP_ADMINS in ident.group_dns
    assert "admins" in ident.group_names  # CN, for name-match sync


def test_search_then_bind_wrong_password_rejected():
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "alice", "WRONG", connector=make_connector()
    )
    assert ident is None


def test_unknown_user_rejected():
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "nobody", "x", connector=make_connector()
    )
    assert ident is None


def test_direct_bind_success():
    cfg = _cfg(
        ldap_user_dn_template="uid={username},ou=people,dc=ex,dc=com",
        ldap_bind_dn=None,
        ldap_bind_password=None,
        ldap_user_base=None,
    )
    ident = ldap_auth.resolve_ldap_identity(cfg, "bob", "bobpw", connector=make_connector())
    assert ident is not None
    assert ident.subject == "bbbbbbbb-1111-2222-3333-444444444444"
    assert ident.username == "bob"


def test_direct_bind_wrong_password_rejected():
    cfg = _cfg(
        ldap_user_dn_template="uid={username},ou=people,dc=ex,dc=com",
        ldap_bind_dn=None,
        ldap_bind_password=None,
        ldap_user_base=None,
    )
    ident = ldap_auth.resolve_ldap_identity(cfg, "bob", "nope", connector=make_connector())
    assert ident is None


def test_subject_falls_back_to_dn_without_uuid_attr():
    # Attribute absent → subject is the DN (with a logged warning), never empty.
    dit = _default_dit()
    del dit[ALICE_DN]["entryUUID"]
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "alice", "alicepw", connector=make_connector(dit)
    )
    assert ident is not None
    assert ident.subject == ALICE_DN


def test_memberof_mode_groups():
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(ldap_use_memberof=True, ldap_group_base=None),
        "alice",
        "alicepw",
        connector=make_connector(),
    )
    assert ident is not None
    assert ident.group_dns == (GRP_ADMINS,)


# --------------------------------------------------------------------------- #
# Security: injection, empty password, anonymous bind                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "evil",
    ["*", "alice)(uid=*", "*)(|(uid=*", "alice\x00", "alice)(|(objectClass=*", "\\2a"],
)
def test_filter_injection_never_bypasses(evil):
    # A crafted username must NOT wildcard/expand into a match for a real user.
    calls: list = []
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), evil, "alicepw", connector=make_connector(calls=calls)
    )
    assert ident is None
    # Only the service search bind may have run; no USER bind for an injected DN.
    user_binds = [u for (u, _p) in calls if u and u.startswith("uid=")]
    assert user_binds == []


def test_dn_injection_in_direct_bind_template_escaped():
    cfg = _cfg(
        ldap_user_dn_template="uid={username},ou=people,dc=ex,dc=com",
        ldap_bind_dn=None,
        ldap_bind_password=None,
        ldap_user_base=None,
    )
    # Comma would break out of the RDN if unescaped; escaping keeps it one value
    # → the DN does not resolve → no bind success.
    ident = ldap_auth.resolve_ldap_identity(
        cfg, "alice,ou=admins", "alicepw", connector=make_connector()
    )
    assert ident is None


def test_empty_password_rejected_before_any_bind():
    calls: list = []
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "alice", "", connector=make_connector(calls=calls)
    )
    assert ident is None
    assert calls == []  # the server was NEVER contacted (no anonymous impersonation)


def test_empty_username_rejected():
    calls: list = []
    ident = ldap_auth.resolve_ldap_identity(
        _cfg(), "", "whatever", connector=make_connector(calls=calls)
    )
    assert ident is None
    assert calls == []


# --------------------------------------------------------------------------- #
# Transport policy (StartTLS-required / plaintext refusal)                     #
# --------------------------------------------------------------------------- #
def test_plaintext_remote_refused_without_starttls():
    with pytest.raises(LDAPError) as ei:
        _cfg(
            ldap_server="ldap://ad.corp.example.com",
            ldap_start_tls=False,
            ldap_allow_plaintext=False,
        )
    assert ei.value.reason == "insecure_transport"


def test_remote_ldap_upgrades_to_starttls_by_default():
    cfg = _cfg(ldap_server="ldap://ad.corp.example.com")
    assert cfg.transport == "starttls"


def test_ldaps_is_implicit_tls():
    cfg = _cfg(ldap_server="ldaps://ad.corp.example.com")
    assert cfg.transport == "ldaps" and cfg.port == 636


def test_plaintext_remote_allowed_with_explicit_optin():
    cfg = _cfg(
        ldap_server="ldap://ad.corp.example.com",
        ldap_start_tls=False,
        ldap_allow_plaintext=True,
    )
    assert cfg.transport == "plaintext"


# --------------------------------------------------------------------------- #
# Role mapping                                                                 #
# --------------------------------------------------------------------------- #
def test_role_map_highest_privilege_wins():
    cfg = _cfg(ldap_role_map=f"{GRP_ADMINS}=>admin;{GRP_STAFF}=>user")
    assert ldap_auth.resolve_role(cfg, (GRP_STAFF, GRP_ADMINS)) == "admin"


def test_role_refuse_when_unmapped_and_no_default():
    cfg = _cfg(ldap_default_role="")
    assert ldap_auth.resolve_role(cfg, ("cn=other,dc=ex,dc=com",)) is None


def test_role_default_applies_when_unmapped():
    cfg = _cfg(ldap_default_role="viewer")
    assert ldap_auth.resolve_role(cfg, ("cn=other,dc=ex,dc=com",)) == "viewer"


def test_role_map_dn_case_insensitive():
    cfg = _cfg(ldap_role_map=f"{GRP_ADMINS.upper()}=>admin")
    assert ldap_auth.resolve_role(cfg, (GRP_ADMINS,)) == "admin"


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
        await conn.execute(text("DELETE FROM principal_group_members"))
        await conn.execute(text("DELETE FROM principal_groups"))
        await conn.execute(text("DELETE FROM sessions"))
        await conn.execute(text("DELETE FROM users"))
        await conn.execute(text("DELETE FROM principals"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def ldap_settings():
    get_settings.cache_clear()
    s = get_settings()
    s.auth_enabled = True
    s.ldap_enabled = True
    s.ldap_server = "ldap://localhost"
    s.ldap_bind_dn = ADMIN_DN
    s.ldap_bind_password = "svcpw"
    s.ldap_user_base = "ou=people,dc=ex,dc=com"
    s.ldap_user_filter = "(uid={username})"
    s.ldap_attr_username = "uid"
    s.ldap_attr_email = "mail"
    s.ldap_attr_uid = "entryUUID"
    s.ldap_group_base = "ou=groups,dc=ex,dc=com"
    s.ldap_group_filter = "(member={user_dn})"
    s.ldap_role_map = f"{GRP_ADMINS}=>admin;{GRP_STAFF}=>user"
    s.ldap_default_role = ""
    s.ldap_group_sync = True
    s.ldap_user_dn_template = None
    s.ldap_auto_provision = True
    yield s
    get_settings.cache_clear()


async def _provision(maker, identity, cfg=None):
    async with maker() as s:
        res = await ldap_auth.provision_ldap_principal(s, cfg or _cfg(), identity)
        await s.commit()
        return res


def _identity(sub="aaaa", username="alice", email="alice@ex.com", group_dns=(GRP_ADMINS,)):
    return ldap_auth.LdapIdentity(
        subject=sub,
        username=username,
        email=email,
        group_dns=tuple(group_dns),
        group_names=tuple(ldap_auth._dn_cn(d) for d in group_dns),
    )


# --------------------------------------------------------------------------- #
# JIT provisioning                                                             #
# --------------------------------------------------------------------------- #
async def test_jit_provision_creates_sso_only_user(db_maker, ldap_settings):
    from sqlalchemy import select

    res = await _provision(db_maker, _identity())
    async with db_maker() as s:
        user = (await s.execute(select(User))).scalar_one()
        principal = await s.get(Principal, user.principal_id)
    assert user.auth_provider == "ldap"
    assert user.password_hash is None  # SSO-only: no local password login
    assert user.external_issuer == "localhost"
    assert user.external_subject == "aaaa"
    assert principal.global_role == "admin"
    # A brand-new principal is created already at its mapped role, so role_changed
    # is False (it flags a CHANGE to an existing principal); nothing is cached
    # under a not-yet-existing id, so no bump is needed on creation.
    assert res.role_changed is False


async def test_jit_username_collision_suffix(db_maker, ldap_settings):
    from sqlalchemy import select

    # Pre-create a LOCAL user named 'alice'.
    async with db_maker() as s:
        p = Principal(kind="user", global_role="viewer")
        s.add(p)
        await s.flush()
        s.add(User(principal_id=p.id, username="alice", auth_provider="local", password_hash="x"))
        await s.commit()
    await _provision(db_maker, _identity())
    async with db_maker() as s:
        names = sorted((await s.execute(select(User.username))).scalars().all())
    assert names == ["alice", "alice1"]


async def test_existing_identity_role_updates_on_next_login(db_maker, ldap_settings):
    await _provision(db_maker, _identity(group_dns=(GRP_ADMINS,)))  # admin
    res2 = await _provision(db_maker, _identity(group_dns=(GRP_STAFF,)))  # now user
    assert res2.role_changed is True
    from sqlalchemy import select

    async with db_maker() as s:
        p = (await s.execute(select(Principal))).scalars().one()
    assert p.global_role == "user"


async def test_no_auto_provision_refuses(db_maker, ldap_settings):
    cfg = _cfg(ldap_auto_provision=False)
    with pytest.raises(LDAPError) as ei:
        await _provision(db_maker, _identity(), cfg=cfg)
    assert ei.value.reason == "no_account"


async def test_provision_refuses_when_role_unmapped(db_maker, ldap_settings):
    cfg = _cfg(ldap_default_role="")
    with pytest.raises(LDAPError) as ei:
        await _provision(db_maker, _identity(group_dns=("cn=nobody,dc=ex,dc=com",)), cfg=cfg)
    assert ei.value.reason == "no_role"


async def test_disabled_principal_refused(db_maker, ldap_settings):
    from datetime import UTC, datetime

    from sqlalchemy import select

    await _provision(db_maker, _identity())
    async with db_maker() as s:
        p = (await s.execute(select(Principal))).scalars().one()
        p.disabled_at = datetime.now(UTC)
        await s.commit()
    with pytest.raises(LDAPError) as ei:
        await _provision(db_maker, _identity())
    assert ei.value.reason == "disabled"


async def test_group_sync_adds_and_removes_ldap_groups(db_maker, ldap_settings):
    from sqlalchemy import select

    async with db_maker() as s:
        s.add(PrincipalGroup(name="admins", source="ldap"))
        s.add(PrincipalGroup(name="staff", source="ldap"))
        s.add(PrincipalGroup(name="manual", source="local"))
        await s.commit()
    # Login 1: admins + manual asserted (manual matches a local group by name).
    ident1 = _identity(group_dns=(GRP_ADMINS, "cn=manual,dc=ex,dc=com"))
    await _provision(db_maker, ident1)
    async with db_maker() as s:
        p = (await s.execute(select(Principal))).scalars().one()
        names1 = set(
            (
                await s.execute(
                    select(PrincipalGroup.name)
                    .join(PrincipalGroupMember, PrincipalGroup.id == PrincipalGroupMember.group_id)
                    .where(PrincipalGroupMember.principal_id == p.id)
                )
            ).scalars().all()
        )
    assert names1 == {"admins", "manual"}
    # Login 2: only staff asserted → ldap-sourced 'admins' removed, local 'manual'
    # kept (never auto-removed), 'staff' added.
    await _provision(db_maker, _identity(group_dns=(GRP_STAFF,)))
    async with db_maker() as s:
        p = (await s.execute(select(Principal))).scalars().one()
        names2 = set(
            (
                await s.execute(
                    select(PrincipalGroup.name)
                    .join(PrincipalGroupMember, PrincipalGroup.id == PrincipalGroupMember.group_id)
                    .where(PrincipalGroupMember.principal_id == p.id)
                )
            ).scalars().all()
        )
    assert names2 == {"staff", "manual"}


# --------------------------------------------------------------------------- #
# Endpoint flow (/auth/login fall-through)                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(db_maker, ldap_settings, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    # Replace the directory I/O seam with the offline MOCK connector (capture the
    # real authenticate_ldap first so the wrapper doesn't recurse into itself).
    conn = make_connector()
    real_auth = ldap_auth.authenticate_ldap

    async def _fake_auth(session, username, password, *, connector=None):
        return await real_auth(session, username, password, connector=conn)

    monkeypatch.setattr(ldap_auth, "authenticate_ldap", _fake_auth)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker


async def test_status_reports_ldap_enabled(client):
    c, _ = client
    r = await c.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["ldap_enabled"] is True


async def test_login_via_ldap_mints_session(client):
    c, maker = client
    r = await c.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepw"})
    assert r.status_code == 200, r.text
    assert r.json()["principal"]["global_role"] == "admin"
    assert "filearr_session" in r.headers.get("set-cookie", "")


async def test_login_ldap_wrong_password_401(client):
    c, _ = client
    r = await c.post("/api/v1/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401


async def test_local_first_same_named_admin_stays_local(client):
    # A LOCAL account named 'alice' must authenticate locally and NEVER fall
    # through to LDAP (even though 'alice' exists in the directory too).
    c, maker = client
    from filearr import authx

    async with maker() as s:
        p = Principal(kind="user", global_role="admin")
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username="alice",
                auth_provider="local",
                password_hash=authx.hash_password("localpw"),
            )
        )
        await s.commit()
    # LDAP password must NOT work (local account blocks the fall-through)...
    r_bad = await c.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepw"})
    assert r_bad.status_code == 401
    # ...the local password does.
    r_ok = await c.post("/api/v1/auth/login", json={"username": "alice", "password": "localpw"})
    assert r_ok.status_code == 200
    async with maker() as s:
        from sqlalchemy import select

        u = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
    assert u.auth_provider == "local"


async def test_login_ldap_bumps_grant_cache_on_group_change(client, monkeypatch):
    c, maker = client
    from filearr import grant_cache

    # Pre-create the 'admins' group so the LDAP group sync joins it on login →
    # groups_changed=True → the login path invalidates the grant cache (P6-T4).
    async with maker() as s:
        s.add(PrincipalGroup(name="admins", source="ldap"))
        await s.commit()

    bumps = {"n": 0}
    orig = grant_cache.bump_generation

    def _count():
        bumps["n"] += 1
        orig()

    monkeypatch.setattr(grant_cache, "bump_generation", _count)
    r = await c.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepw"})
    assert r.status_code == 200
    assert bumps["n"] >= 1
