"""P10-T12 — central ``agent_share_maps`` fallback: pure longest-prefix resolution
matrix (agent-vs-global precedence, separator-safe, UI-T15 UNC-derivation
consistency), migration round-trip, and the admin CRUD surface (create/list/get/
patch/delete + validation: credential-URL & bad-scheme 422, dup-prefix 409,
unknown agent/library 404, FILEARR_AGENTS_ENABLED gate-off 404, admin-scope gate,
audit rows).

Runs against the migrated pgserver Postgres (mirrors test_agent_commands's
harness). The pure resolver tests need no DB.
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
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentShareMap, Library
from filearr.share_map import (
    ShareLocation,
    _location_from_prefix,
    resolve_for_agent,
)
from filearr.transfers import ShareMapping, resolve_share_url

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure resolution matrix (no DB)                                               #
# --------------------------------------------------------------------------- #
def test_longest_prefix_wins():
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/data", agent_id="A"),
        ShareMapping(
            local_prefix="/data/media", share_prefix="smb://nas/media", agent_id="A"
        ),
    ]
    # the more-specific /data/media wins over /data
    assert (
        resolve_share_url(maps, "A", "/data/media/movie.mkv")
        == "smb://nas/media/movie.mkv"
    )
    # a path only /data covers falls to the shorter prefix
    assert resolve_share_url(maps, "A", "/data/other/x") == "smb://nas/data/other/x"


def test_no_match_returns_none():
    maps = [ShareMapping(local_prefix="/data", share_prefix="smb://nas/data", agent_id="A")]
    assert resolve_share_url(maps, "A", "/elsewhere/x") is None
    assert resolve_for_agent(maps, "A", "/elsewhere/x") is None


def test_agent_scoped_does_not_leak_to_other_agents():
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/A", agent_id="A"),
    ]
    assert resolve_share_url(maps, "A", "/data/x") == "smb://nas/A/x"
    # agent B has no covering mapping -> no link (no leak of A's mapping)
    assert resolve_share_url(maps, "B", "/data/x") is None


def test_global_none_matches_any_agent():
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/any", agent_id=None),
    ]
    assert resolve_share_url(maps, "A", "/data/x") == "smb://nas/any/x"
    assert resolve_share_url(maps, "Z", "/data/x") == "smb://nas/any/x"
    assert resolve_share_url(maps, None, "/data/x") == "smb://nas/any/x"


def test_agent_specific_outranks_global_at_equal_length():
    """A concrete-agent rule and a global rule with the SAME local_prefix length
    resolve deterministically to the agent-specific one (doc accept criterion)."""
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/global", agent_id=None),
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/agentA", agent_id="A"),
    ]
    assert resolve_share_url(maps, "A", "/data/x") == "smb://nas/agentA/x"
    # order-independent
    assert resolve_share_url(list(reversed(maps)), "A", "/data/x") == "smb://nas/agentA/x"
    # a different agent still gets the global rule
    assert resolve_share_url(maps, "B", "/data/x") == "smb://nas/global/x"


def test_longer_global_beats_shorter_agent():
    """Longest-prefix is the primary key; agent-specificity only breaks a TIE."""
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/agentA", agent_id="A"),
        ShareMapping(
            local_prefix="/data/media", share_prefix="smb://nas/global-media", agent_id=None
        ),
    ]
    assert (
        resolve_share_url(maps, "A", "/data/media/x") == "smb://nas/global-media/x"
    )


def test_separator_safe_windows_local_prefix():
    maps = [
        ShareMapping(
            local_prefix="C:\\media", share_prefix="\\\\nas\\media", agent_id="A"
        ),
    ]
    # a windows agent path resolves against the UNC share, native backslashes
    assert (
        resolve_share_url(maps, "A", "C:\\media\\Movies\\a.mkv")
        == "\\\\nas\\media\\Movies\\a.mkv"
    )


def test_segment_boundary_not_substring():
    """/data must NOT match /database (segment boundary, not raw prefix)."""
    maps = [ShareMapping(local_prefix="/data", share_prefix="smb://nas/data", agent_id="A")]
    assert resolve_share_url(maps, "A", "/database/x") is None


def test_unc_derivation_consistency_with_ui_t15():
    """resolve_for_agent's URL+UNC pair matches the UI-T15 derivation exactly."""
    # smb:// prefix -> unc derived
    maps = [
        ShareMapping(local_prefix="/data", share_prefix="smb://nas/media", agent_id="A")
    ]
    loc = resolve_for_agent(maps, "A", "/data/sub/x.mkv")
    assert loc == ShareLocation(url="smb://nas/media/sub/x.mkv", unc="\\\\nas\\media\\sub\\x.mkv")
    # the joined URL run through the shared UI-T15 helper agrees byte-for-byte
    assert loc == _location_from_prefix("smb://nas/media/sub/x.mkv")

    # UNC prefix -> smb URL derived; url field is the URL form, unc the windows form
    maps2 = [
        ShareMapping(
            local_prefix="C:\\m", share_prefix="\\\\nas\\media", agent_id="A"
        )
    ]
    loc2 = resolve_for_agent(maps2, "A", "C:\\m\\a\\b.mkv")
    assert loc2 == ShareLocation(url="smb://nas/media/a/b.mkv", unc="\\\\nas\\media\\a\\b.mkv")

    # non-SMB scheme (sftp) -> no UNC counterpart
    maps3 = [
        ShareMapping(local_prefix="/data", share_prefix="sftp://host/path", agent_id="A")
    ]
    loc3 = resolve_for_agent(maps3, "A", "/data/x")
    assert loc3 == ShareLocation(url="sftp://host/path/x", unc=None)


def test_explicit_unc_is_used_verbatim():
    """A mapping carrying an explicit unc joins it directly (not derived)."""
    maps = [
        ShareMapping(
            local_prefix="/data",
            share_prefix="smb://nas/media",
            unc="\\\\pinned\\share",
            agent_id="A",
        )
    ]
    loc = resolve_for_agent(maps, "A", "/data/x.mkv")
    assert loc.url == "smb://nas/media/x.mkv"
    assert loc.unc == "\\\\pinned\\share\\x.mkv"


# --------------------------------------------------------------------------- #
# DB harness (mirrors test_agent_commands)                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_share_maps"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker) -> tuple[uuid.UUID, uuid.UUID]:
    """Create an agent + a library. Returns (agent_id, library_id)."""
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux")
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add_all([agent, lib])
        await s.commit()
        return agent.id, lib.id


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
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
        yield c, maker, settings
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Migration round-trip + constraints                                          #
# --------------------------------------------------------------------------- #
async def test_migration_round_trip_and_unique(db_maker):
    agent_id, lib_id = await _seed(db_maker)
    async with db_maker() as s:
        s.add(
            AgentShareMap(
                agent_id=agent_id,
                library_id=lib_id,
                local_prefix="/data",
                share_prefix="smb://nas/data",
            )
        )
        await s.commit()
        got = (
            await s.execute(text("SELECT local_prefix, share_prefix FROM agent_share_maps"))
        ).one()
        assert got.local_prefix == "/data"
    # UNIQUE NULLS NOT DISTINCT (agent_id, library_id, local_prefix): a second
    # identical (agent_id, library_id, prefix) row is rejected by the DB.
    with pytest.raises(Exception):  # noqa: B017
        async with db_maker() as s:
            s.add(
                AgentShareMap(
                    agent_id=agent_id,
                    library_id=lib_id,
                    local_prefix="/data",
                    share_prefix="smb://nas/other",
                )
            )
            await s.commit()


async def test_null_agent_global_row_persists(db_maker):
    _agent, _lib = await _seed(db_maker)
    async with db_maker() as s:
        s.add(AgentShareMap(local_prefix="/data", share_prefix="smb://nas/any"))
        await s.commit()
        n = (await s.execute(text("SELECT count(*) FROM agent_share_maps"))).scalar()
        assert n == 1


# --------------------------------------------------------------------------- #
# CRUD + validation                                                           #
# --------------------------------------------------------------------------- #
async def test_create_list_get_patch_delete(client):
    c, maker, _ = client
    agent_id, lib_id = await _seed(maker)
    # create
    r = await c.post(
        f"/api/v1/agents/{agent_id}/share-maps",
        json={
            "library_id": str(lib_id),
            "local_prefix": "/data/media/",  # trailing slash normalised off
            "share_prefix": "smb://nas/media",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    mid = body["id"]
    assert body["local_prefix"] == "/data/media"  # normalised
    # both-format preview present (UI-T15)
    assert body["location"]["url"] == "smb://nas/media"
    assert body["location"]["unc"] == "\\\\nas\\media"

    # list + filters
    lst = (await c.get("/api/v1/agent-share-maps")).json()
    assert len(lst) == 1 and lst[0]["id"] == mid
    assert (await c.get("/api/v1/agent-share-maps", params={"agent_id": str(agent_id)})).json()
    assert (
        await c.get("/api/v1/agent-share-maps", params={"agent_id": str(uuid.uuid4())})
    ).json() == []

    # get
    one = await c.get(f"/api/v1/agent-share-maps/{mid}")
    assert one.status_code == 200 and one.json()["share_prefix"] == "smb://nas/media"

    # patch
    p = await c.patch(
        f"/api/v1/agent-share-maps/{mid}", json={"share_prefix": "sftp://host/p"}
    )
    assert p.status_code == 200, p.text
    assert p.json()["share_prefix"] == "sftp://host/p"
    assert p.json()["location"]["unc"] is None  # sftp has no UNC

    # delete
    d = await c.delete(f"/api/v1/agent-share-maps/{mid}")
    assert d.status_code == 204
    assert (await c.get("/api/v1/agent-share-maps")).json() == []


async def test_credential_url_rejected(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/share-maps",
        json={"local_prefix": "/data", "share_prefix": "smb://user:pass@nas/media"},
    )
    assert r.status_code == 422, r.text
    assert "credential" in r.text.lower()


async def test_bad_scheme_rejected(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    for bad in ("http://nas/x", "javascript:alert(1)", "not-a-path"):
        r = await c.post(
            f"/api/v1/agents/{agent_id}/share-maps",
            json={"local_prefix": "/data", "share_prefix": bad},
        )
        assert r.status_code == 422, (bad, r.text)


async def test_allowed_schemes_and_unc_and_posix(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    for i, sp in enumerate(
        ["smb://h/s", "sftp://h/p", "ftp://h/p", "nfs://h/e", "webdav://h/w",
         "\\\\host\\share", "/Volumes/media"]
    ):
        r = await c.post(
            f"/api/v1/agents/{agent_id}/share-maps",
            json={"local_prefix": f"/data/{i}", "share_prefix": sp},
        )
        assert r.status_code == 201, (sp, r.text)


async def test_duplicate_prefix_409(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    payload = {"local_prefix": "/data", "share_prefix": "smb://nas/data"}
    assert (await c.post(f"/api/v1/agents/{agent_id}/share-maps", json=payload)).status_code == 201
    dup = await c.post(f"/api/v1/agents/{agent_id}/share-maps", json=payload)
    assert dup.status_code == 409, dup.text
    # normalised trailing slash is still a dup
    dup2 = await c.post(
        f"/api/v1/agents/{agent_id}/share-maps",
        json={"local_prefix": "/data/", "share_prefix": "smb://nas/data"},
    )
    assert dup2.status_code == 409


async def test_unknown_agent_and_library_404(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{uuid.uuid4()}/share-maps",
        json={"local_prefix": "/data", "share_prefix": "smb://nas/data"},
    )
    assert r.status_code == 404
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/share-maps",
        json={
            "local_prefix": "/data",
            "share_prefix": "smb://nas/data",
            "library_id": str(uuid.uuid4()),
        },
    )
    assert r2.status_code == 404


async def test_audit_rows_written(client):
    c, maker, _ = client
    agent_id, _ = await _seed(maker)
    mid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/share-maps",
            json={"local_prefix": "/data", "share_prefix": "smb://nas/data"},
        )
    ).json()["id"]
    await c.patch(f"/api/v1/agent-share-maps/{mid}", json={"host": "nas"})
    await c.delete(f"/api/v1/agent-share-maps/{mid}")
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT event_type FROM security_events ORDER BY ts")
            )
        ).scalars().all()
    assert "agent_share_map_created" in rows
    assert "agent_share_map_updated" in rows
    assert "agent_share_map_deleted" in rows


# --------------------------------------------------------------------------- #
# Feature gate + admin scope gate                                             #
# --------------------------------------------------------------------------- #
async def test_gate_off_404(client, monkeypatch):
    c, maker, settings = client
    agent_id, _ = await _seed(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.get("/api/v1/agent-share-maps")).status_code == 404
    assert (
        await c.post(
            f"/api/v1/agents/{agent_id}/share-maps",
            json={"local_prefix": "/data", "share_prefix": "smb://nas/data"},
        )
    ).status_code == 404


async def test_admin_scope_required(db_maker, monkeypatch):
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
        # admin mutation without a bearer key -> 401
        assert (
            await c.post(
                f"/api/v1/agents/{uuid.uuid4()}/share-maps",
                json={"local_prefix": "/data", "share_prefix": "smb://nas/data"},
            )
        ).status_code == 401
        # read surface likewise requires a key
        assert (await c.get("/api/v1/agent-share-maps")).status_code == 401
    app.dependency_overrides.clear()
