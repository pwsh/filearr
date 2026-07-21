"""P5-T6 — agent config/policy push (central-side): the policy_versions table +
resolution precedence, the agent poll plane (ETag/If-None-Match/304 + applied=
stamping + bearer auth), the admin write/list/history plane (append-only
versioning + validation matrix + audit + admin/gate parity).

Runs against the migrated pgserver Postgres (mirrors test_agent_commands's harness).
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
from filearr import taxonomy
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, PolicyVersion
from filearr.policy import (
    PolicyValidationError,
    ScopeError,
    parse_scope,
    resolve_effective_policy,
    scope_string,
    validate_policy,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# DB harness                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM policy_versions"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_agent(maker, rollout_group: str = "default") -> tuple[uuid.UUID, str]:
    """Create an ACTIVE agent (bound fingerprint). Returns (agent_id, fingerprint)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            rollout_group=rollout_group,
            cert_fingerprint=fp,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


async def _add_policy(maker, scope_type, scope_id, version, policy) -> None:
    async with maker() as s:
        s.add(
            PolicyVersion(
                scope_type=scope_type,
                scope_id=scope_id,
                version=version,
                policy=policy,
            )
        )
        await s.commit()


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


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


async def _tax_version(maker) -> int:
    """The current taxonomy_state.version. Read dynamically because the migrated
    Postgres is session-shared, so an earlier taxonomy-editing test may have
    advanced it — the W8-E policy ETag/body carry whatever the live version is."""
    async with maker() as s:
        return int(
            (
                await s.execute(text("SELECT version FROM taxonomy_state WHERE id = 1"))
            ).scalar_one()
        )


async def _restore_tax(maker, version: int) -> None:
    """Undo a taxonomy edit made by a test (delete the probe ext, reset the version
    counter) so the session-shared DB stays net-unchanged for later tests that
    assert an absolute taxonomy version (e.g. test_taxonomy_w8)."""
    async with maker() as s:
        await s.execute(text("DELETE FROM file_group_extensions WHERE ext = 'zzz'"))
        await s.execute(
            text("UPDATE taxonomy_state SET version = :v WHERE id = 1"), {"v": version}
        )
        await s.commit()
    taxonomy.invalidate()


# --------------------------------------------------------------------------- #
# Pure — scope grammar + validation                                            #
# --------------------------------------------------------------------------- #
def test_parse_and_stringify_scope_roundtrip():
    aid = uuid.uuid4()
    assert parse_scope("global") == ("global", None)
    assert parse_scope("group:canary") == ("group", "canary")
    assert parse_scope(f"agent:{aid}") == ("agent", str(aid))
    assert scope_string("global", None) == "global"
    assert scope_string("group", "canary") == "group:canary"
    assert scope_string("agent", str(aid)) == f"agent:{aid}"
    # round-trip
    for s in ("global", "group:g1", f"agent:{aid}"):
        st, sid = parse_scope(s)
        assert scope_string(st, sid) == s


@pytest.mark.parametrize(
    "bad", ["banana", "", "group:", "agent:not-a-uuid", "wat:x", "agent:", ":x"]
)
def test_parse_scope_rejects_malformed(bad):
    with pytest.raises(ScopeError):
        parse_scope(bad)


def test_validate_policy_matrix():
    # empty / valid known keys pass
    validate_policy({})
    validate_policy(
        {
            "presets": ["system_files"],
            "include_globs": ["*.mkv"],
            "exclude_globs": ["*.tmp"],
            "content_hash_max_bytes": 0,
            "watch_mode": True,
            "reconcile_interval_seconds": 300,
            "poll_interval_seconds": 60,
        }
    )
    # unknown keys pass (preserved by the caller)
    validate_policy({"future_key": {"nested": 1}, "watch_mode": False})
    # non-object
    with pytest.raises(PolicyValidationError):
        validate_policy(["not", "an", "object"])
    # bad preset name
    with pytest.raises(PolicyValidationError):
        validate_policy({"presets": ["nope_not_real"]})
    # bounds
    with pytest.raises(PolicyValidationError):
        validate_policy({"content_hash_max_bytes": -1})
    with pytest.raises(PolicyValidationError):
        validate_policy({"reconcile_interval_seconds": 299})
    with pytest.raises(PolicyValidationError):
        validate_policy({"poll_interval_seconds": 59})
    with pytest.raises(PolicyValidationError):
        validate_policy({"poll_interval_seconds": 86401})


# --------------------------------------------------------------------------- #
# Pure-ish — resolution precedence (agent > group > global; none)              #
# --------------------------------------------------------------------------- #
async def _resolve(maker, agent_id):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await resolve_effective_policy(s, agent)


async def test_resolution_agent_wins(db_maker):
    agent_id, _ = await _seed_agent(db_maker, rollout_group="g1")
    await _add_policy(db_maker, "global", None, 1, {"lvl": "global"})
    await _add_policy(db_maker, "group", "g1", 1, {"lvl": "group"})
    await _add_policy(db_maker, "agent", str(agent_id), 1, {"lvl": "agent"})
    scope, ver, pol = await _resolve(db_maker, agent_id)
    assert scope == f"agent:{agent_id}" and ver == 1 and pol["lvl"] == "agent"


async def test_resolution_group_when_no_agent_row(db_maker):
    agent_id, _ = await _seed_agent(db_maker, rollout_group="g1")
    await _add_policy(db_maker, "global", None, 1, {"lvl": "global"})
    await _add_policy(db_maker, "group", "g1", 1, {"lvl": "group"})
    scope, ver, pol = await _resolve(db_maker, agent_id)
    assert scope == "group:g1" and pol["lvl"] == "group"


async def test_resolution_global_when_no_group_row(db_maker):
    agent_id, _ = await _seed_agent(db_maker, rollout_group="g1")
    # a group policy for a DIFFERENT group must not leak in
    await _add_policy(db_maker, "group", "other", 1, {"lvl": "other-group"})
    await _add_policy(db_maker, "global", None, 2, {"lvl": "global"})
    scope, ver, pol = await _resolve(db_maker, agent_id)
    assert scope == "global" and ver == 2 and pol["lvl"] == "global"


async def test_resolution_none_when_empty(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    scope, ver, pol = await _resolve(db_maker, agent_id)
    assert (scope, ver, pol) == ("none", 0, {})


async def test_resolution_picks_max_version(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _add_policy(db_maker, "global", None, 1, {"v": 1})
    await _add_policy(db_maker, "global", None, 3, {"v": 3})
    await _add_policy(db_maker, "global", None, 2, {"v": 2})
    scope, ver, pol = await _resolve(db_maker, agent_id)
    assert ver == 3 and pol["v"] == 3


# --------------------------------------------------------------------------- #
# Agent plane — GET /agents/{id}/policy (ETag / 304 / applied= / none-case)     #
# --------------------------------------------------------------------------- #
async def test_policy_none_case(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    tv = await _tax_version(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.status_code == 200
    # W8-E: central injects the live taxonomy_version into every policy doc and
    # folds it into the ETag as a /t:<v> suffix.
    assert r.json() == {"scope": "none", "version": 0, "policy": {"taxonomy_version": tv}}
    assert r.headers["etag"] == f'"none/0/t:{tv}"'


async def test_policy_200_with_etag(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await c.put("/api/v1/agent-policies/global", json={"policy": {"watch_mode": True}})
    tv = await _tax_version(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "scope": "global",
        "version": 1,
        "policy": {"watch_mode": True, "taxonomy_version": tv},
    }
    assert r.headers["etag"] == f'"global/1/t:{tv}"'


async def test_policy_304_roundtrip(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await c.put("/api/v1/agent-policies/global", json={"policy": {"watch_mode": True}})
    first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    etag = first.headers["etag"]
    again = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag},
    )
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert again.content == b""


async def test_scope_flip_invalidates_etag(client):
    """global v3 ETag goes stale the instant an agent-scope row appears."""
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    for _ in range(3):  # global v1..v3
        await c.put("/api/v1/agent-policies/global", json={"policy": {"g": True}})
    tv = await _tax_version(maker)
    poll = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert poll.headers["etag"] == f'"global/3/t:{tv}"'
    # a more-specific agent-scope policy now exists
    await c.put(
        f"/api/v1/agent-policies/agent:{agent_id}", json={"policy": {"a": True}}
    )
    # re-poll with the OLD (global/3) validator -> NOT 304, new agent-scope etag
    reptr = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": f'"global/3/t:{tv}"'},
    )
    assert reptr.status_code == 200
    assert reptr.headers["etag"] == f'"agent:{agent_id}/1/t:{tv}"'
    assert reptr.json()["policy"] == {"a": True, "taxonomy_version": tv}


async def test_applied_stamps_agent(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await c.get(f"/api/v1/agents/{agent_id}/policy?applied=7", headers=_auth(fp))
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.policy_version_applied == 7
        assert a.last_seen_at is not None


async def test_policy_requires_agent_credential(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    assert (await c.get(f"/api/v1/agents/{agent_id}/policy")).status_code == 401
    bad = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth("nope"))
    assert bad.status_code == 401


# --------------------------------------------------------------------------- #
# Admin plane — write (append-only) / list / history / validation / audit       #
# --------------------------------------------------------------------------- #
async def test_put_appends_versions(client):
    c, maker, _ = client
    r1 = await c.put("/api/v1/agent-policies/global", json={"policy": {"n": 1}})
    assert r1.status_code == 200 and r1.json()["version"] == 1
    r2 = await c.put("/api/v1/agent-policies/global", json={"policy": {"n": 2}})
    assert r2.json()["version"] == 2
    assert r2.json()["scope"] == "global"
    # old row is NOT mutated — both versions persist
    async with maker() as s:
        rows = (await s.execute(text("SELECT version FROM policy_versions ORDER BY version"))).all()
        assert [r.version for r in rows] == [1, 2]


async def test_unknown_key_passthrough(client):
    c, maker, _ = client
    payload = {"future_key": {"deep": [1, 2]}, "watch_mode": True}
    r = await c.put("/api/v1/agent-policies/global", json={"policy": payload})
    assert r.status_code == 200
    assert r.json()["policy"] == payload  # stored verbatim
    async with maker() as s:
        stored = (await s.execute(text("SELECT policy FROM policy_versions"))).scalar_one()
        assert stored == payload


async def test_put_list_current(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    await c.put("/api/v1/agent-policies/global", json={"policy": {"n": 1}})
    await c.put("/api/v1/agent-policies/global", json={"policy": {"n": 2}})
    await c.put("/api/v1/agent-policies/group:canary", json={"policy": {"g": 1}})
    await c.put(f"/api/v1/agent-policies/agent:{agent_id}", json={"policy": {"a": 1}})
    lst = (await c.get("/api/v1/agent-policies")).json()
    by_scope = {row["scope"]: row for row in lst}
    assert set(by_scope) == {"global", "group:canary", f"agent:{agent_id}"}
    assert by_scope["global"]["version"] == 2  # current (max) per scope
    assert by_scope["global"]["policy"] == {"n": 2}


async def test_history_desc_and_cap(client):
    c, _, _ = client
    for _ in range(3):
        await c.put("/api/v1/agent-policies/global", json={"policy": {"x": 1}})
    hist = (await c.get("/api/v1/agent-policies/global/history")).json()
    assert [r["version"] for r in hist] == [3, 2, 1]
    # limit + keyset before
    limited = (await c.get("/api/v1/agent-policies/global/history?limit=1")).json()
    assert [r["version"] for r in limited] == [3]
    before = (await c.get("/api/v1/agent-policies/global/history?before=3")).json()
    assert [r["version"] for r in before] == [2, 1]


async def test_put_bad_scope_422(client):
    c, _, _ = client
    for bad in ("banana", "group:", "agent:not-a-uuid"):
        r = await c.put(f"/api/v1/agent-policies/{bad}", json={"policy": {}})
        assert r.status_code == 422, bad


async def test_put_unknown_agent_422(client):
    c, _, _ = client
    r = await c.put(
        f"/api/v1/agent-policies/agent:{uuid.uuid4()}", json={"policy": {}}
    )
    assert r.status_code == 422


async def test_put_bad_preset_422(client):
    c, _, _ = client
    r = await c.put(
        "/api/v1/agent-policies/global", json={"policy": {"presets": ["nope"]}}
    )
    assert r.status_code == 422


async def test_put_non_object_policy_422(client):
    c, _, _ = client
    r = await c.put("/api/v1/agent-policies/global", json={"policy": "not-an-object"})
    assert r.status_code == 422


async def test_put_oversize_413(client):
    c, _, settings = client
    big = {"blob": "x" * (settings.agent_policy_max_bytes + 100)}
    r = await c.put("/api/v1/agent-policies/global", json={"policy": big})
    assert r.status_code == 413


async def test_put_emits_audit_without_body(client):
    c, maker, _ = client
    await c.put(
        "/api/v1/agent-policies/global", json={"policy": {"secret_glob": "x" * 50}}
    )
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT event_type, details FROM security_events "
                    "WHERE event_type = 'agent_policy_updated'"
                )
            )
        ).one()
    assert row.event_type == "agent_policy_updated"
    assert row.details == {"scope": "global", "version": 1}  # scope+version only
    assert "secret_glob" not in str(row.details)  # never the body


# --------------------------------------------------------------------------- #
# Feature-gate parity (404 when FILEARR_AGENTS_ENABLED is off)                  #
# --------------------------------------------------------------------------- #
async def test_feature_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).status_code == 404
    assert (
        await c.put("/api/v1/agent-policies/global", json={"policy": {}})
    ).status_code == 404
    assert (await c.get("/api/v1/agent-policies")).status_code == 404
    assert (
        await c.get("/api/v1/agent-policies/global/history")
    ).status_code == 404


# --------------------------------------------------------------------------- #
# W8-E — taxonomy_version in the policy doc/ETag + agent-plane taxonomy endpoint #
# --------------------------------------------------------------------------- #
async def test_policy_taxonomy_version_folds_into_etag_and_bumps(client):
    """A taxonomy edit invalidates the agent's policy cache: the /t:<v> ETag
    suffix advances and the policy body's taxonomy_version follows."""
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    await c.put("/api/v1/agent-policies/global", json={"policy": {"watch_mode": True}})
    start = await _tax_version(maker)
    try:
        first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
        assert first.json()["policy"]["taxonomy_version"] == start
        assert first.headers["etag"] == f'"global/1/t:{start}"'

        # A matching validator is a 304 before the edit.
        again = await c.get(
            f"/api/v1/agents/{agent_id}/policy",
            headers={**_auth(fp), "If-None-Match": f'"global/1/t:{start}"'},
        )
        assert again.status_code == 304

        # Edit the taxonomy (add an ext to a group) -> version bumps by one.
        r = await c.post(
            "/api/v1/taxonomy/groups/raster-photo/extensions", json={"ext": "zzz"}
        )
        assert r.status_code == 200 and r.json()["version"] == start + 1

        # The OLD validator no longer 304s; the new doc/ETag carry the new version.
        after = await c.get(
            f"/api/v1/agents/{agent_id}/policy",
            headers={**_auth(fp), "If-None-Match": f'"global/1/t:{start}"'},
        )
        assert after.status_code == 200
        assert after.headers["etag"] == f'"global/1/t:{start + 1}"'
        assert after.json()["policy"]["taxonomy_version"] == start + 1
    finally:
        await _restore_tax(maker, start)


async def test_agent_taxonomy_endpoint_shape(client):
    """The compact agent taxonomy payload: version + flat maps + primary set."""
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    tv = await _tax_version(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "version",
        "ext_to_group",
        "group_to_category",
        "category_extractor",
        "primary_categories",
    }
    assert body["version"] == tv
    # A known extension resolves to the right group + category.
    assert body["ext_to_group"]["mkv"] == "video"
    assert body["ext_to_group"]["flac"] == "audio-lossless"
    assert body["group_to_category"]["video"] == "video"
    assert body["group_to_category"]["audio-lossless"] == "audio"
    # Extractor map: media-ish categories route to a pipeline; others are null.
    assert body["category_extractor"]["image"] == "image"
    assert body["category_extractor"]["three-d-cad"] == "model3d"
    assert body["category_extractor"]["archive"] is None
    # Primary = the categories with an extractor (sidecar-parent eligibility).
    assert body["primary_categories"] == [
        "image",
        "audio",
        "video",
        "document",
        "three-d-cad",
    ]


async def test_agent_taxonomy_endpoint_reflects_edit(client):
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    start = await _tax_version(maker)
    try:
        await c.post(
            "/api/v1/taxonomy/groups/raster-photo/extensions", json={"ext": "zzz"}
        )
        r = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
        assert r.json()["version"] == start + 1
        assert r.json()["ext_to_group"]["zzz"] == "raster-photo"
    finally:
        await _restore_tax(maker, start)


async def test_agent_taxonomy_requires_agent_credential(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    assert (await c.get(f"/api/v1/agents/{agent_id}/taxonomy")).status_code == 401
    bad = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth("nope"))
    assert bad.status_code == 401


async def test_agent_taxonomy_feature_gated(client, monkeypatch):
    c, maker, settings = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (
        await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
    ).status_code == 404
