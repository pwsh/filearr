"""P6-T4 — route-level RBAC enforcement: ``require_permission`` + SQL
``scope_where_clause`` + the two-tier grant cache.

Integration tests against the migrated Postgres (pgserver text-column sandbox):

* the SQL scope clause equals ``rbac.evaluate`` on random grant sets (executed
  against seeded rows — the property test that binds the SQL path to the pure
  engine);
* the item-endpoint enforcement matrix (allow / deny / no-grant / admin /
  auth-off) and the 404-vs-403 ruling;
* report EXPORT scoping (a denied row is absent from the CSV/NDJSON stream);
* per-request grant memoization + process-cache invalidation (DB-fetch count);
* the NFC/NFD-sibling grant-creation warning; and
* transfer-endpoint RBAC gating.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, grant_cache, rbac, rbac_sql
from filearr import db as db_mod
from filearr.api import items as items_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Item,
    Library,
    MediaType,
    PathGrant,
    Principal,
    User,
)

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
            "path_grants",
            "principal_group_members",
            "principal_groups",
            "sessions",
            "items",
            "libraries",
            "users",
            "principals",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    # Reset the process grant cache so a prior module's entries never bleed in.
    grant_cache._cache.clear()
    grant_cache.bump_generation()
    yield m
    await engine.dispose()


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    async def _noop_defer(*a, **k):
        return None

    monkeypatch.setattr(items_mod, "defer_index_sync", _noop_defer)
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
        yield c, settings
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Seed helpers                                                                 #
# --------------------------------------------------------------------------- #
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


def _item(lib, rel):
    return Item(
        library_id=lib.id,
        media_type=MediaType.video,
        path=f"/data/{lib.name}/{rel}",
        rel_path=rel,
        filename=rel.rsplit("/", 1)[-1],
        extension=rel.rsplit(".", 1)[-1],
        size=random.randint(1, 10_000),
        mtime=datetime.now(UTC),
        metadata_={},
        user_metadata={},
        external_ids={},
        tags=[],
        path_scope=rbac.path_to_ltree(rel, library_id=lib.id),
    )


async def _grant(maker, subject_id, library_id, rel, action, effect="allow", kind="principal"):
    async with maker() as s:
        scope = (
            rbac.library_label(library_id)
            if not rel
            else rbac.path_to_ltree(rel, library_id=library_id)
        )
        s.add(
            PathGrant(
                subject_kind=kind,
                subject_id=subject_id,
                library_id=library_id,
                scope=scope,
                action=action,
                effect=effect,
            )
        )
        await s.commit()
    # Mirror the API's cache invalidation for direct-DB seeding (the real
    # POST /rbac/grants handler bumps the generation on every mutation).
    grant_cache.bump_generation()


async def _login(c, username, password="pw-123456", **headers):
    r = await c.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r


# --------------------------------------------------------------------------- #
# 1. SQL scope_where_clause == rbac.evaluate (property, executed on pg)        #
# --------------------------------------------------------------------------- #
async def test_sql_clause_matches_evaluate_over_random_grants(maker):
    async with maker() as s:
        lib = Library(name="movies", root_path="/data/movies")
        s.add(lib)
        await s.flush()
        rels = [
            "a/one.mkv",
            "a/b/two.mkv",
            "a/b/c/three.mkv",
            "a/secret/four.mkv",
            "x/five.mkv",
            "y/z/six.mkv",
        ]
        items = [_item(lib, r) for r in rels]
        s.add_all(items)
        await s.commit()
        lib_id = lib.id
        scoped = [(it.id, it.path_scope) for it in items]

    scope_prefixes = ["a", "a/b", "a/b/c", "a/secret", "x", "y/z", ""]
    rnd = random.Random(20260713)
    use_ltree = False  # sandbox column is text
    for _ in range(40):
        grants = []
        for _ in range(rnd.randint(0, 5)):
            rel = rnd.choice(scope_prefixes)
            scope = (
                rbac.library_label(lib_id)
                if not rel
                else rbac.path_to_ltree(rel, library_id=lib_id)
            )
            grants.append(
                rbac.PathGrant(
                    path=scope,
                    action="search_metadata",
                    allow=rnd.random() < 0.65,
                )
            )
        role = rnd.choice([rbac.Role.USER, rbac.Role.VIEWER])
        clause = rbac_sql.scope_where_clause(
            role, grants, action="search_metadata", column=Item.path_scope, use_ltree=use_ltree
        )
        # Expected set via the pure engine.
        expected = {
            iid
            for iid, ps in scoped
            if rbac.evaluate(grants, role, ps, "search_metadata").allowed
        }
        async with maker() as s:
            if clause is None:
                got = {r for (r,) in (await s.execute(select(Item.id))).all()}
            else:
                got = {
                    r
                    for (r,) in (
                        await s.execute(select(Item.id).where(clause))
                    ).all()
                }
        assert got == expected, (role, [(g.path, g.allow) for g in grants])


async def test_admin_clause_is_none_and_ceiling_clamped_is_false():
    admin_clause = rbac_sql.scope_where_clause(
        rbac.Role.ADMIN, [], action="download", column=Item.path_scope, use_ltree=False
    )
    assert admin_clause is None  # unrestricted
    # viewer + download exceeds the ceiling -> matches nothing.
    g = [rbac.PathGrant(path="lib_x", action="download", allow=True)]
    clause = rbac_sql.scope_where_clause(
        rbac.Role.VIEWER, g, action="download", column=Item.path_scope, use_ltree=False
    )
    assert clause is not None
    assert str(clause.compile(compile_kwargs={"literal_binds": True})) == "false"


# --------------------------------------------------------------------------- #
# 2. Item endpoint enforcement matrix + 404-vs-403                            #
# --------------------------------------------------------------------------- #
async def _two_libs(maker):
    async with maker() as s:
        movies = Library(name="movies", root_path="/data/movies")
        music = Library(name="music", root_path="/data/music")
        s.add_all([movies, music])
        await s.flush()
        mv = _item(movies, "action/film.mkv")
        secret = _item(movies, "secret/hidden.mkv")
        mu = _item(music, "rock/song.mp3")
        s.add_all([mv, secret, mu])
        await s.commit()
        return {
            "movies": movies.id,
            "music": music.id,
            "mv": mv.id,
            "secret": secret.id,
            "mu": mu.id,
        }


async def test_item_get_matrix_and_404_no_leak(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "reader", role="user")
    await _grant(maker, uid, ids["movies"], "", "search_metadata", "allow")
    await _grant(maker, uid, ids["movies"], "secret", "search_metadata", "deny")

    await _login(c, "reader")
    # granted subtree -> 200
    r = await c.get(f"/api/v1/items/{ids['mv']}")
    assert r.status_code == 200
    # explicitly denied subtree -> 404 (never leak existence)
    r = await c.get(f"/api/v1/items/{ids['secret']}")
    assert r.status_code == 404
    # un-granted library -> 404
    r = await c.get(f"/api/v1/items/{ids['mu']}")
    assert r.status_code == 404


async def test_item_get_admin_and_no_grant(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    await _mk_user(maker, "boss", role="admin")
    await _mk_user(maker, "nogrant", role="user")

    await _login(c, "boss")
    for key in ("mv", "secret", "mu"):
        assert (await c.get(f"/api/v1/items/{ids[key]}")).status_code == 200

    await _login(c, "nogrant")
    for key in ("mv", "secret", "mu"):
        assert (await c.get(f"/api/v1/items/{ids[key]}")).status_code == 404


async def test_patch_404_vs_403(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "editor", role="user")
    # readable but NOT edit_metadata on movies; nothing on music.
    await _grant(maker, uid, ids["movies"], "", "search_metadata", "allow")

    await _login(c, "editor")
    # readable, action denied -> 403
    r = await c.patch(f"/api/v1/items/{ids['mv']}", json={"title": "X"})
    assert r.status_code == 403
    # unreadable -> 404 (no existence leak)
    r = await c.patch(f"/api/v1/items/{ids['mu']}", json={"title": "X"})
    assert r.status_code == 404

    # now grant edit_metadata on movies -> 200
    await _grant(maker, uid, ids["movies"], "", "edit_metadata", "allow")
    r = await c.patch(f"/api/v1/items/{ids['mv']}", json={"title": "X"})
    assert r.status_code == 200


async def test_auth_off_is_open(client, maker, monkeypatch):
    c, settings = client
    ids = await _two_libs(maker)
    monkeypatch.setattr(settings, "auth_enabled", False)
    # No credentials at all -> open (legacy behaviour, byte-identical).
    for key in ("mv", "secret", "mu"):
        assert (await c.get(f"/api/v1/items/{ids[key]}")).status_code == 200


# --------------------------------------------------------------------------- #
# 3. Report export scoping                                                     #
# --------------------------------------------------------------------------- #
async def test_report_export_requires_download_action(client, maker):
    # P11-T10: search_metadata lets a scoped principal VIEW a report (JSON) but
    # not EXPORT it — an export requires the stronger `download` action.
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "viewonly", role="user")
    await _grant(maker, uid, ids["movies"], "", "search_metadata", "allow")

    await _login(c, "viewonly")
    # JSON screen-view: allowed (search_metadata).
    assert (await c.get("/api/v1/reports/largest_files")).status_code == 200
    # Export: denied without the download action.
    for fmt in ("csv", "ndjson", "xml", "xlsx"):
        r = await c.get("/api/v1/reports/largest_files", params={"format": fmt})
        assert r.status_code == 403, fmt


async def test_report_csv_export_omits_denied_rows(client, maker):
    # With the `download` action granted, the export streams — scoped to the
    # rows the principal may DOWNLOAD (P11-T10).
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "analyst", role="user")
    await _grant(maker, uid, ids["movies"], "", "download", "allow")

    await _login(c, "analyst")
    r = await c.get("/api/v1/reports/largest_files", params={"format": "csv"})
    assert r.status_code == 200
    body = r.text
    assert "action/film.mkv" in body      # granted (download)
    assert "rock/song.mp3" not in body    # un-granted, must be absent
    assert "secret/hidden.mkv" in body    # movies subtree readable (no deny here)


async def test_report_ndjson_export_scoped(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "nd", role="user")
    await _grant(maker, uid, ids["music"], "", "download", "allow")

    await _login(c, "nd")
    r = await c.get("/api/v1/reports/largest_files", params={"format": "ndjson"})
    assert r.status_code == 200
    assert "rock/song.mp3" in r.text
    assert "action/film.mkv" not in r.text


# --------------------------------------------------------------------------- #
# 4. Grant cache: per-request memo + process cache + invalidation             #
# --------------------------------------------------------------------------- #
async def test_grant_cache_memo_and_invalidation(client, maker, monkeypatch):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "cached", role="user")
    await _grant(maker, uid, ids["movies"], "", "search_metadata", "allow")
    await _login(c, "cached")

    calls = {"n": 0}
    real = grant_cache.load_principal_grants

    async def _spy(session, principal_id):
        calls["n"] += 1
        return await real(session, principal_id)

    monkeypatch.setattr(grant_cache, "load_principal_grants", _spy)

    # copies uses the grants TWICE in one request (authorize_item + sql_clause):
    # still exactly one DB fetch (per-request memo).
    calls["n"] = 0
    r = await c.get(f"/api/v1/items/{ids['mv']}/copies")
    assert r.status_code == 200
    assert calls["n"] == 1

    # a second request within the TTL is served from the process cache: 0 fetches.
    calls["n"] = 0
    r = await c.get(f"/api/v1/items/{ids['mv']}/copies")
    assert r.status_code == 200
    assert calls["n"] == 0

    # a grant mutation bumps the generation -> next request refetches.
    grant_cache.bump_generation()
    calls["n"] = 0
    r = await c.get(f"/api/v1/items/{ids['mv']}/copies")
    assert r.status_code == 200
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 5. NFC/NFD sibling warning (non-blocking)                                    #
# --------------------------------------------------------------------------- #
async def test_nfc_sibling_grant_warning(client, maker):
    c, _ = client
    async with maker() as s:
        lib = Library(name="movies", root_path="/data/movies")
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    pid = await _mk_user(maker, "admin", role="admin")
    await _login(c, "admin")

    nfc = "Café"          # é as one code point (U+00E9)
    nfd = "Café"         # e + combining acute (U+0301)
    r1 = await c.post(
        "/api/v1/rbac/grants",
        json={
            "subject_kind": "principal",
            "subject_id": str(pid),
            "library_id": str(lib_id),
            "rel_path": nfc,
            "action": "search_metadata",
        },
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["warnings"] == []

    r2 = await c.post(
        "/api/v1/rbac/grants",
        json={
            "subject_kind": "principal",
            "subject_id": str(pid),
            "library_id": str(lib_id),
            "rel_path": nfd,
            "action": "search_metadata",
        },
    )
    assert r2.status_code == 201, r2.text
    warns = r2.json()["warnings"]
    assert warns and "sibling" in warns[0]


# --------------------------------------------------------------------------- #
# 6. Transfer endpoint RBAC gating                                             #
# --------------------------------------------------------------------------- #
async def test_transfer_initiate_rbac_gate(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "mover", role="user")
    # download granted on movies only.
    await _grant(maker, uid, ids["movies"], "", "download", "allow")
    await _login(c, "mover")

    body = {}
    # movies item: download allowed -> passes the RBAC gate, reaches the live
    # handler; the seed lib is centrally-scanned (non-agent-hosted) so it 422s
    # (past the gate — the gate did NOT block it).
    r = await c.post(f"/api/v1/items/{ids['mv']}/transfer", json=body)
    assert r.status_code == 422
    # music item: outside read scope -> 404 (existence not leaked, gate first).
    r = await c.post(f"/api/v1/items/{ids['mu']}/transfer", json=body)
    assert r.status_code == 404


async def test_transfer_initiate_denied_action_403(client, maker):
    c, _ = client
    ids = await _two_libs(maker)
    uid = await _mk_user(maker, "vieweronly", role="user")
    # readable but no download grant -> 403 (readable, action denied).
    await _grant(maker, uid, ids["movies"], "", "search_metadata", "allow")
    await _login(c, "vieweronly")
    r = await c.post(f"/api/v1/items/{ids['mv']}/transfer", json={})
    assert r.status_code == 403
