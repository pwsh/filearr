"""P6-T2 — RBAC core: migration round-trip, groups/grants CRUD, ceiling
enforcement at grant creation, the decision-preview endpoint (mirroring the pure
``rbac.evaluate`` matrix through the API), and the path_scope backfill.

Runs against the migrated pgserver Postgres. ltree is unavailable in the sandbox
build (no contrib) — the migration falls back to a ``text`` column + btree; the
round-trip test asserts the shape that actually landed and, when ltree IS
present, exercises the native ``<@`` ancestor operator + GIST index.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import rbac
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Item,
    Library,
    PathGrant,
    Principal,
    User,
)

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _has_ltree_sync(pg_uri: str) -> bool:
    import psycopg

    with psycopg.connect(pg_uri) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name='ltree'"
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM auth_rate_limits"))
        await conn.execute(text("DELETE FROM path_grants"))
        await conn.execute(text("DELETE FROM principal_group_members"))
        await conn.execute(text("DELETE FROM principal_groups"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM users"))
        await conn.execute(text("DELETE FROM principals"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    # Auth disabled: exercises the RBAC endpoints' own logic (require_scope
    # no-ops). Admin-gating is covered by test_auth_p6's require_scope suite.
    monkeypatch.setattr(settings, "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _mk_user(maker, username, role="user") -> uuid.UUID:
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        s.add(User(principal_id=p.id, username=username, auth_provider="local"))
        await s.commit()
        return p.id


async def _mk_library(maker, name="movies") -> uuid.UUID:
    async with maker() as s:
        lib = Library(name=name, root_path=f"/data/{name}")
        s.add(lib)
        await s.commit()
        return lib.id


# --------------------------------------------------------------------------- #
# Migration round-trip (ltree ext + GIST, or the text fallback)               #
# --------------------------------------------------------------------------- #
async def test_migration_round_trip_scope_and_index(db_maker, pg_uri):
    has_ltree = _has_ltree_sync(pg_uri)
    async with db_maker() as s:
        # tables exist + a dotted scope string round-trips through the column.
        lib = Library(name="lib", root_path="/data/lib")
        s.add(lib)
        await s.flush()
        scope = rbac.path_to_ltree("Action/2020", library_id=lib.id)
        g = PathGrant(
            subject_kind="group",
            subject_id=uuid.uuid4(),
            library_id=lib.id,
            scope=scope,
            action="search_metadata",
            effect="allow",
        )
        s.add(g)
        from datetime import UTC, datetime

        it = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/lib/Action/2020/f.mkv",
            rel_path="Action/2020/f.mkv",
            filename="f.mkv",
            size=1,
            mtime=datetime.now(UTC),
            path_scope=rbac.path_to_ltree("Action/2020/f.mkv", library_id=lib.id),
        )
        s.add(it)
        await s.commit()

        got = (
            await s.execute(text("SELECT scope::text FROM path_grants LIMIT 1"))
        ).scalar_one()
        assert got == scope

    # The scope-index landed under the expected name for this environment.
    async with db_maker() as s:
        idx = (
            await s.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename='path_grants'")
            )
        ).scalars().all()
        if has_ltree:
            assert "ix_path_grants_scope_gist" in idx
            # native ltree ancestor query works.
            hit = (
                await s.execute(
                    text(
                        "SELECT 1 FROM items WHERE path_scope <@ "
                        "(SELECT scope FROM path_grants LIMIT 1)"
                    )
                )
            ).first()
            assert hit is not None
        else:
            assert "ix_path_grants_scope" in idx


# --------------------------------------------------------------------------- #
# Groups + membership CRUD                                                     #
# --------------------------------------------------------------------------- #
async def test_group_crud_and_membership(client):
    c, maker = client
    uid = await _mk_user(maker, "alice")

    r = await c.post("/api/v1/rbac/groups", json={"name": "Editors", "description": "d"})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    # duplicate name rejected
    assert (await c.post("/api/v1/rbac/groups", json={"name": "Editors"})).status_code == 409

    # add member
    assert (
        await c.post(f"/api/v1/rbac/groups/{gid}/members", json={"principal_id": str(uid)})
    ).status_code == 204
    members = (await c.get(f"/api/v1/rbac/groups/{gid}/members")).json()
    assert [m["username"] for m in members] == ["alice"]

    # member_count reflected
    groups = (await c.get("/api/v1/rbac/groups")).json()
    assert groups[0]["member_count"] == 1

    # idempotent re-add, then remove
    await c.post(f"/api/v1/rbac/groups/{gid}/members", json={"principal_id": str(uid)})
    assert len((await c.get(f"/api/v1/rbac/groups/{gid}/members")).json()) == 1
    assert (
        await c.delete(f"/api/v1/rbac/groups/{gid}/members/{uid}")
    ).status_code == 204
    assert (await c.get(f"/api/v1/rbac/groups/{gid}/members")).json() == []

    # delete group
    assert (await c.delete(f"/api/v1/rbac/groups/{gid}")).status_code == 204
    assert (await c.get("/api/v1/rbac/groups")).json() == []


# --------------------------------------------------------------------------- #
# Grant creation validation + ceiling enforcement                             #
# --------------------------------------------------------------------------- #
async def test_grant_validation_and_ceiling(client):
    c, maker = client
    lib = await _mk_library(maker)
    viewer = await _mk_user(maker, "vic", role="viewer")
    user = await _mk_user(maker, "uma", role="user")
    grp = (await c.post("/api/v1/rbac/groups", json={"name": "G"})).json()

    # unknown action -> 422
    r = await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "group", "subject_id": grp["id"], "library_id": str(lib),
        "rel_path": "Action", "action": "teleport", "effect": "allow"})
    assert r.status_code == 422

    # missing library -> 404
    r = await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "group", "subject_id": grp["id"],
        "library_id": str(uuid.uuid4()), "action": "search_metadata"})
    assert r.status_code == 404

    # viewer principal + modify -> rejected at CREATION (ceiling)
    r = await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "principal", "subject_id": str(viewer), "library_id": str(lib),
        "rel_path": "Action", "action": "modify", "effect": "allow"})
    assert r.status_code == 422
    assert "ceiling" in r.text

    # user principal + modify -> allowed (within ceiling)
    r = await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "principal", "subject_id": str(user), "library_id": str(lib),
        "rel_path": "Action", "action": "modify", "effect": "allow"})
    assert r.status_code == 201, r.text
    # server encoded the scope from (library, rel_path) — never raw ltree.
    assert r.json()["scope"] == rbac.path_to_ltree("Action", library_id=lib)

    # whole-library grant (blank rel_path) -> library-root scope
    r = await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "group", "subject_id": grp["id"], "library_id": str(lib),
        "action": "search_metadata"})
    assert r.status_code == 201
    assert r.json()["scope"] == rbac.library_label(lib)

    grants = (await c.get("/api/v1/rbac/grants")).json()
    assert len(grants) == 2
    # delete one
    assert (await c.delete(f"/api/v1/rbac/grants/{grants[0]['id']}")).status_code == 204


# --------------------------------------------------------------------------- #
# Preview endpoint — mirror the pure evaluate matrix through the API           #
# --------------------------------------------------------------------------- #
async def _seed_matrix(c, maker):
    """A user in a group granted allow@movies.Action, deny@movies.Action.2020."""
    lib = await _mk_library(maker, "movies")
    uid = await _mk_user(maker, "grace", role="user")
    grp = (await c.post("/api/v1/rbac/groups", json={"name": "Watchers"})).json()
    await c.post(f"/api/v1/rbac/groups/{grp['id']}/members", json={"principal_id": str(uid)})
    await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "group", "subject_id": grp["id"], "library_id": str(lib),
        "rel_path": "Action", "action": "search_metadata", "effect": "allow"})
    await c.post("/api/v1/rbac/grants", json={
        "subject_kind": "group", "subject_id": grp["id"], "library_id": str(lib),
        "rel_path": "Action/2020", "action": "search_metadata", "effect": "deny"})
    return lib, uid


async def _preview(c, principal, library, path, action="search_metadata"):
    r = await c.get("/api/v1/rbac/preview", params={
        "principal": str(principal), "library": str(library),
        "path": path, "action": action})
    assert r.status_code == 200, r.text
    return r.json()


async def test_preview_matrix(client):
    c, maker = client
    lib, uid = await _seed_matrix(c, maker)

    # under the allowed subtree -> allowed
    d = await _preview(c, uid, lib, "Action/1999/film.mkv")
    assert d["allowed"] is True and d["reason"] == "explicit_allow"
    assert d["winning_grant"]["effect"] == "allow"

    # under the deny subtree -> denied by longest-prefix deny
    d = await _preview(c, uid, lib, "Action/2020/film.mkv")
    assert d["allowed"] is False and d["reason"] == "explicit_deny"

    # a sibling not covered by any grant -> default deny
    d = await _preview(c, uid, lib, "Comedy/film.mkv")
    assert d["allowed"] is False and d["reason"] == "no_grant_default_deny"

    # an action outside the granted one -> default deny (grant is action-scoped)
    d = await _preview(c, uid, lib, "Action/1999/film.mkv", action="download")
    assert d["allowed"] is False


async def test_preview_admin_bypass_and_ceiling(client):
    c, maker = client
    lib = await _mk_library(maker, "movies")
    admin = await _mk_user(maker, "adam", role="admin")
    viewer = await _mk_user(maker, "vera", role="viewer")

    # admin: bypass regardless of grants
    d = await _preview(c, admin, lib, "anything/here.mkv", action="delete")
    assert d["allowed"] is True and d["reason"] == "admin_bypass"

    # viewer asking for a write action -> ceiling clamp (no grant can rescue it)
    d = await _preview(c, viewer, lib, "x.mkv", action="modify")
    assert d["allowed"] is False and d["reason"] == "ceiling_clamped"

    # unknown principal -> 404
    r = await c.get("/api/v1/rbac/preview", params={
        "principal": str(uuid.uuid4()), "library": str(lib),
        "path": "x", "action": "search_metadata"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Backfill endpoint                                                            #
# --------------------------------------------------------------------------- #
async def test_rbac_backfill_stamps_and_batches(client, monkeypatch):
    c, maker = client
    lib = await _mk_library(maker, "movies")
    from datetime import UTC, datetime

    # Seed items with NULL path_scope (as a pre-P6 catalog would have).
    rels = [f"Action/{i}/film.mkv" for i in range(5)]
    async with maker() as s:
        for r in rels:
            s.add(Item(
                library_id=lib, file_category="video", file_group="video",
                path=f"/data/movies/{r}", rel_path=r, filename="film.mkv",
                size=1, mtime=datetime.now(UTC),
            ))
        await s.commit()

    # Force a tiny batch so we exercise the keyset loop across >1 page.
    import filearr.api.system as system_mod

    monkeypatch.setattr(system_mod, "RBAC_BACKFILL_BATCH", 2)

    r = await c.post("/api/v1/system/rbac-backfill")
    assert r.status_code == 200
    assert r.json()["stamped"] == 5

    async with maker() as s:
        items = (
            await s.execute(text("SELECT rel_path, path_scope::text FROM items ORDER BY rel_path"))
        ).all()
    for rel, scope in items:
        assert scope == rbac.path_to_ltree(rel, library_id=lib)

    # Idempotent: a second run stamps nothing (all non-NULL now).
    assert (await c.post("/api/v1/system/rbac-backfill")).json()["stamped"] == 0


async def test_db_action_check_matches_rbac_actions(db_maker):
    """Defense-in-depth: the ``path_grants.action`` CHECK constraint rejects an
    action outside ``rbac.ACTIONS`` at the DB layer (not just the API). Also
    proves every real action IS accepted, so the DDL list and ``rbac.ACTIONS``
    can never silently drift apart."""
    import sqlalchemy as sa

    async with db_maker() as s:
        lib = Library(name="lib", root_path="/data/lib")
        s.add(lib)
        await s.flush()
        # a bogus action is rejected by the CHECK
        with pytest.raises(Exception):  # noqa: PT011,B017
            await s.execute(
                sa.text(
                    "INSERT INTO path_grants (subject_kind, subject_id, library_id, "
                    "scope, action, effect) VALUES "
                    "('group', gen_random_uuid(), :lib, 'lib_x', 'teleport', 'allow')"
                ),
                {"lib": lib.id},
            )
        await s.rollback()

    # every rbac.ACTIONS value is accepted by the CHECK
    async with db_maker() as s:
        lib = Library(name="lib2", root_path="/data/lib2")
        s.add(lib)
        await s.flush()
        for act in sorted(rbac.ACTIONS):
            s.add(PathGrant(
                subject_kind="group", subject_id=uuid.uuid4(), library_id=lib.id,
                scope="lib_x", action=act, effect="allow",
            ))
        await s.commit()
        n = (await s.execute(text("SELECT count(*) FROM path_grants"))).scalar_one()
        assert n == len(rbac.ACTIONS)
