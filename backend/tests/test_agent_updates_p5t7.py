"""P5-T7 — signed agent update manifest + staged rollout (central-side).

Central STORES/serves the signed manifest but never verifies the signature (the
agent does that against its pinned key — Go-side test). These endpoint tests
cover: the FILEARR_AGENTS_ENABLED gate, two-phase upload (register manifest +
stream artifacts with sha256 verification), canary→general coverage + the
promote gate, the up-to-date 204 path, artifact download with no traversal, and
the confirmed-version rollup.

Runs against the migrated pgserver Postgres (mirrors test_agent_commands' harness).
"""

from __future__ import annotations

import hashlib
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
from filearr.models import Agent

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_releases"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "agent_releases_dir", str(tmp_path / "releases"))
    monkeypatch.setattr(settings, "agent_canary_group", "canary")
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, rollout_group="default", version=None) -> tuple[uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name="a",
            hostname="a",
            platform="linux",
            rollout_group=rollout_group,
            cert_fingerprint=fp,
            agent_version=version,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _auth(fp: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fp}"}


async def _poll(c, aid, fp, current):
    return await c.get(
        f"/api/v1/agents/{aid}/update-manifest?current={current}", headers=_auth(fp)
    )


def _manifest(version: str, artifacts: list[tuple[str, str, bytes, str]]) -> dict:
    return {
        "version": version,
        "created_at": "2026-07-17T12:00:00Z",
        "artifacts": [
            {
                "platform": p,
                "arch": a,
                "sha256": hashlib.sha256(b).hexdigest(),
                "size": len(b),
                "url": fn,
            }
            for (p, a, b, fn) in artifacts
        ],
        "signature": "ZmFrZS1zaWduYXR1cmU=",  # central never checks it
    }


async def _upload_release(c, version, artifacts) -> None:
    """Register a manifest then stream each artifact (the full two-phase upload)."""
    m = _manifest(version, artifacts)
    r = await c.post("/api/v1/agent-releases", json=m)
    assert r.status_code == 201, r.text
    for (_p, _a, b, fn) in artifacts:
        r = await c.put(f"/api/v1/agent-releases/{version}/artifacts/{fn}", content=b)
        assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Feature gate                                                                 #
# --------------------------------------------------------------------------- #
async def test_feature_gate_404(client, monkeypatch):
    c, maker, settings = client
    monkeypatch.setattr(settings, "agents_enabled", False)
    r = await c.post("/api/v1/agent-releases", json={})
    assert r.status_code == 404
    aid, fp = await _seed_agent(maker)
    r = await c.get(f"/api/v1/agents/{aid}/update-manifest?current=1.0.0", headers=_auth(fp))
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Upload + sha256 verification                                                 #
# --------------------------------------------------------------------------- #
async def test_upload_and_sha256_verification(client):
    c, _maker, _settings = client
    art = b"NEW-BINARY-BYTES"
    m = _manifest("1.5.0", [("linux", "amd64", art, "agent-linux-amd64")])
    r = await c.post("/api/v1/agent-releases", json=m)
    assert r.status_code == 201
    body = r.json()
    assert body["stage"] == "canary" and body["ready"] is False

    # Corrupted bytes -> 400 (central-side upload integrity), file not stored.
    r = await c.put("/api/v1/agent-releases/1.5.0/artifacts/agent-linux-amd64", content=b"CORRUPT")
    assert r.status_code == 400

    # Correct bytes -> 200; release now ready.
    r = await c.put("/api/v1/agent-releases/1.5.0/artifacts/agent-linux-amd64", content=art)
    assert r.status_code == 200
    r = await c.get("/api/v1/agent-releases")
    rel = next(x for x in r.json()["releases"] if x["version"] == "1.5.0")
    assert rel["ready"] is True


async def test_duplicate_version_conflict(client):
    c, _maker, _settings = client
    m = _manifest("1.5.0", [("linux", "amd64", b"x", "a")])
    assert (await c.post("/api/v1/agent-releases", json=m)).status_code == 201
    assert (await c.post("/api/v1/agent-releases", json=m)).status_code == 409


async def test_unsigned_or_bad_manifest_rejected(client):
    c, _maker, _settings = client
    # Missing signature.
    m = _manifest("1.5.0", [("linux", "amd64", b"x", "a")])
    del m["signature"]
    assert (await c.post("/api/v1/agent-releases", json=m)).status_code == 400
    # No artifacts.
    m = _manifest("1.6.0", [])
    assert (await c.post("/api/v1/agent-releases", json=m)).status_code == 400


# --------------------------------------------------------------------------- #
# Staged rollout: canary coverage + promote                                    #
# --------------------------------------------------------------------------- #
async def test_canary_coverage_then_promote(client):
    c, maker, _settings = client
    art = b"BIN"
    await _upload_release(c, "1.5.0", [("linux", "amd64", art, "agent-linux-amd64")])

    canary_id, canary_fp = await _seed_agent(maker, rollout_group="canary", version="1.4.0")
    default_id, default_fp = await _seed_agent(maker, rollout_group="default", version="1.4.0")

    # Canary agent SEES the canary release.
    r = await _poll(c, canary_id, canary_fp, "1.4.0")
    assert r.status_code == 200
    assert r.json()["version"] == "1.5.0"

    # Default-group agent does NOT (204) until promotion.
    r = await _poll(c, default_id, default_fp, "1.4.0")
    assert r.status_code == 204

    # Promote canary -> general.
    r = await c.post("/api/v1/agent-releases/1.5.0/promote")
    assert r.status_code == 200 and r.json()["stage"] == "general"

    # Now EVERYONE sees it.
    r = await _poll(c, default_id, default_fp, "1.4.0")
    assert r.status_code == 200 and r.json()["version"] == "1.5.0"


async def test_promote_incomplete_conflict(client):
    c, _maker, _settings = client
    # Register but do NOT upload the artifact -> not ready -> promote 409.
    m = _manifest("1.5.0", [("linux", "amd64", b"x", "a")])
    await c.post("/api/v1/agent-releases", json=m)
    r = await c.post("/api/v1/agent-releases/1.5.0/promote")
    assert r.status_code == 409
    # Already-general is also a 409.
    await c.put("/api/v1/agent-releases/1.5.0/artifacts/a", content=b"x")
    assert (await c.post("/api/v1/agent-releases/1.5.0/promote")).status_code == 200
    assert (await c.post("/api/v1/agent-releases/1.5.0/promote")).status_code == 409


async def test_up_to_date_204(client):
    c, maker, _settings = client
    await _upload_release(c, "1.5.0", [("linux", "amd64", b"BIN", "agent-linux-amd64")])
    await c.post("/api/v1/agent-releases/1.5.0/promote")
    aid, fp = await _seed_agent(maker, version="1.5.0")
    # Running the same version -> nothing newer.
    r = await c.get(f"/api/v1/agents/{aid}/update-manifest?current=1.5.0", headers=_auth(fp))
    assert r.status_code == 204
    # A newer running version -> still nothing.
    r = await c.get(f"/api/v1/agents/{aid}/update-manifest?current=1.9.0", headers=_auth(fp))
    assert r.status_code == 204


async def test_no_artifact_for_platform_still_served(client):
    # Central serves the manifest regardless of platform coverage; the agent
    # decides whether an artifact matches. So a windows-only manifest is still
    # offered to a linux agent (the agent then finds no artifact). Central's job
    # is coverage + freshness, not platform matching.
    c, maker, _settings = client
    await _upload_release(c, "1.5.0", [("windows", "amd64", b"WIN", "agent-win.exe")])
    await c.post("/api/v1/agent-releases/1.5.0/promote")
    aid, fp = await _seed_agent(maker, version="1.4.0")
    r = await c.get(f"/api/v1/agents/{aid}/update-manifest?current=1.4.0", headers=_auth(fp))
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Artifact download (agent-authed, no traversal)                               #
# --------------------------------------------------------------------------- #
async def test_download_artifact_and_traversal_guard(client):
    c, maker, _settings = client
    art = b"REAL-BINARY-PAYLOAD"
    await _upload_release(c, "1.5.0", [("linux", "amd64", art, "agent-linux-amd64")])
    aid, fp = await _seed_agent(maker)

    r = await c.get(
        f"/api/v1/agents/{aid}/releases/1.5.0/artifacts/agent-linux-amd64", headers=_auth(fp)
    )
    assert r.status_code == 200
    assert r.content == art
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(art).hexdigest()

    # A filename not in the manifest -> 404 (never served off disk).
    r = await c.get(
        f"/api/v1/agents/{aid}/releases/1.5.0/artifacts/not-listed", headers=_auth(fp)
    )
    assert r.status_code == 404

    # Unauthenticated -> 401.
    r = await c.get(f"/api/v1/agents/{aid}/releases/1.5.0/artifacts/agent-linux-amd64")
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Confirmed-version rollup + poll records running version                       #
# --------------------------------------------------------------------------- #
async def test_confirmed_version_rollup(client):
    c, maker, _settings = client
    await _upload_release(c, "1.5.0", [("linux", "amd64", b"BIN", "agent-linux-amd64")])
    await c.post("/api/v1/agent-releases/1.5.0/promote")
    aid, fp = await _seed_agent(maker, version="1.4.0")

    # A manifest poll reporting the running version records it (confirmed signal).
    r = await c.get(f"/api/v1/agents/{aid}/update-manifest?current=1.5.0", headers=_auth(fp))
    assert r.status_code == 204  # already on 1.5.0

    r = await c.get("/api/v1/agent-releases")
    body = r.json()
    ag = next(a for a in body["agents"] if a["id"] == str(aid))
    assert ag["agent_version"] == "1.5.0"
    rel = next(x for x in body["releases"] if x["version"] == "1.5.0")
    assert rel["confirmed_count"] == 1


# --------------------------------------------------------------------------- #
# Admin gating (auth enabled)                                                  #
# --------------------------------------------------------------------------- #
async def test_admin_gated_when_auth_enabled(client, monkeypatch):
    c, _maker, settings = client
    monkeypatch.setattr(settings, "auth_enabled", True)
    m = _manifest("1.5.0", [("linux", "amd64", b"x", "a")])
    # No credentials -> 401/403 (admin scope required), never 201.
    r = await c.post("/api/v1/agent-releases", json=m)
    assert r.status_code in (401, 403)
