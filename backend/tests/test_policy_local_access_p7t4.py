"""P7-T4 — local-query-surface policy keys (central-side).

Covers the additive policy keys the Go agent consumes for its local CLI/web-UI
surface (``local_access_enabled``, ``web_ui_enabled``, ``auth_required``,
``read_only``, ``path_scope``, ``offline_grace_seconds``): pydantic validation
(incl. the fail-closed ``read_only=false`` rejection), the PUT admin plane, the
effective-policy passthrough, and the pure RBAC-grant → predicate flattening
helper.

The API-plane tests reuse the P5-T6 harness shape (migrated Postgres, agents
enabled, auth off).
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
from filearr.models import Agent
from filearr.policy import (
    DEFAULT_OFFLINE_GRACE_SECONDS,
    PathScopeFlattenError,
    PolicyValidationError,
    flatten_path_grants,
    validate_policy,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure — validation of the new keys                                            #
# --------------------------------------------------------------------------- #
def test_local_access_keys_accepted():
    validate_policy(
        {
            "local_access_enabled": False,
            "web_ui_enabled": True,
            "auth_required": False,
            "read_only": True,
            "path_scope": ["Movies/**", "Music/Song.flac"],
            "offline_grace_seconds": 3600,
        }
    )
    # absent (all None) is valid — every key optional
    validate_policy({})


def test_read_only_false_rejected_fail_closed():
    with pytest.raises(PolicyValidationError):
        validate_policy({"read_only": False})
    # read_only: true is fine (the invariant value)
    validate_policy({"read_only": True})


def test_offline_grace_bounds():
    validate_policy({"offline_grace_seconds": 0})  # 0 = fail-closed immediately
    with pytest.raises(PolicyValidationError):
        validate_policy({"offline_grace_seconds": -1})
    assert DEFAULT_OFFLINE_GRACE_SECONDS == 86400  # 24h, R4 reuse


def test_path_scope_validation():
    with pytest.raises(PolicyValidationError):
        validate_policy({"path_scope": ["ok", ""]})  # empty predicate
    with pytest.raises(PolicyValidationError):
        validate_policy({"path_scope": ["ok", "  "]})  # whitespace-only
    with pytest.raises(PolicyValidationError):
        validate_policy({"path_scope": [1, 2]})  # non-string
    with pytest.raises(PolicyValidationError):
        validate_policy({"path_scope": ["x"] * 1001})  # over the count cap


def test_unknown_keys_still_pass_through():
    # P7-T4 must not change the unknown-key behavior (still allowed + preserved).
    validate_policy({"future_local_knob": {"deep": 1}, "web_ui_enabled": True})


def test_upload_rate_bytes_per_sec_bounds():
    # P10-T4 additive key: non-negative int; 0 = unlimited; absent = unlimited.
    validate_policy({"upload_rate_bytes_per_sec": 0})
    validate_policy({"upload_rate_bytes_per_sec": 1_048_576})
    validate_policy({})  # absent is valid (unlimited)
    with pytest.raises(PolicyValidationError):
        validate_policy({"upload_rate_bytes_per_sec": -1})


# --------------------------------------------------------------------------- #
# Pure — RBAC grant → flattened predicate list                                 #
# --------------------------------------------------------------------------- #
def _grant(rel: str | None, lib: uuid.UUID, action="search_metadata", allow=True):
    if rel is None:
        path = rbac.library_label(lib)  # library-root grant
    else:
        path = rbac.path_to_ltree(rel, library_id=lib)
    return rbac.PathGrant(path=path, action=action, allow=allow)


def test_flatten_allow_grant_to_globs():
    lib = uuid.uuid4()
    preds = flatten_path_grants([_grant("Movies/Kids", lib)])
    assert preds == ["Movies/Kids", "Movies/Kids/**"]


def test_flatten_library_root_is_whole_subtree():
    lib = uuid.uuid4()
    assert flatten_path_grants([_grant(None, lib)]) == ["**"]


def test_flatten_dedupes_and_sorts():
    lib = uuid.uuid4()
    preds = flatten_path_grants([_grant("A", lib), _grant("A", lib), _grant("B", lib)])
    assert preds == ["A", "A/**", "B", "B/**"]


def test_flatten_only_read_action_grants():
    lib = uuid.uuid4()
    # a non-read (download) grant is irrelevant to the read-only local surface
    preds = flatten_path_grants([_grant("Movies", lib, action="download")])
    assert preds == []
    # a deny on a NON-read action is ignored (does not fail-close)
    preds = flatten_path_grants(
        [
            _grant("Movies", lib, action="search_metadata", allow=True),
            _grant("Movies", lib, action="delete", allow=False),
        ]
    )
    assert preds == ["Movies", "Movies/**"]


def test_flatten_deny_read_grant_fails_closed():
    lib = uuid.uuid4()
    with pytest.raises(PathScopeFlattenError):
        flatten_path_grants([_grant("Movies", lib, allow=False)])


def test_flatten_hashed_label_fails_closed():
    lib = uuid.uuid4()
    # a very long segment hashes to a one-way ltree label → not reversible → refuse
    with pytest.raises(PathScopeFlattenError):
        flatten_path_grants([_grant("A" * 100, lib)])


# --------------------------------------------------------------------------- #
# API plane — PUT validation + effective serving                               #
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


async def _seed_agent(maker) -> tuple[uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            rollout_group="default",
            cert_fingerprint=fp,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


async def test_put_rejects_read_only_false(client):
    c, _, _ = client
    r = await c.put(
        "/api/v1/agent-policies/global", json={"policy": {"read_only": False}}
    )
    assert r.status_code == 422
    # read_only:true is accepted
    ok = await c.put(
        "/api/v1/agent-policies/global", json={"policy": {"read_only": True}}
    )
    assert ok.status_code == 200


async def test_effective_policy_serves_local_keys_verbatim(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    payload = {
        "local_access_enabled": True,
        "web_ui_enabled": True,
        "auth_required": False,
        "path_scope": ["Movies/**"],
        "offline_grace_seconds": 3600,
    }
    await c.put("/api/v1/agent-policies/global", json={"policy": payload})
    r = await c.get(
        f"/api/v1/agents/{agent_id}/policy", headers={"Authorization": f"Bearer {fp}"}
    )
    assert r.status_code == 200
    assert r.json()["policy"] == payload  # served verbatim through resolution
