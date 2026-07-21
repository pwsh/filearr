"""P10-T9 — audit logging for retrievals (verification + gap-close).

R2: bytes read off another machine are ALWAYS logged, regardless of
``FILEARR_AUDIT_READS``. Two halves:

* the completed **retrieve** (``agent_transfer_downloaded``) — already pinned in
  ``test_transfers_p10t13.py`` (unconditional download audit); and
* the completed **rehash_check** (``agent_verify_completed``) — this file: a rehash
  reads the file's full content on the agent, so its terminal completion writes an
  audit row even with ``audit_reads`` at its default ``False``.

Control (the "an ordinary search writes none under the default" half) is pinned by
``test_security_hardening_p6.py::test_audit_reads_flag_gates_search_event``; here the
scoping control is that a completed **stat_check** (a metadata-only existence probe)
writes NO ``agent_verify_completed`` row — the carve-out is rehash-specific, not a
blanket "audit every agent read".

Reuses the migrated-pgserver harness of ``test_verify_p10t3.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import rbac
from filearr import worker as worker_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Agent,
    ItemStatus,
    Library,
    SecurityEvent,
)
from filearr.models import Item as ItemModel

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
            "agent_commands",
            "security_events",
            "items",
            "libraries",
            "agents",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker):
    """Seed an active (cert-bound) agent + agent-owned library + one active item."""
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path="/data/media",
            source_agent_id=agent.id,
            agent_library_ref="/data/media",
        )
        s.add(lib)
        await s.flush()
        item = ItemModel(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/media/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1000,
            mtime=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            quick_hash="q0",
            content_hash="c0",
            status=ItemStatus.active,
            path_scope=rbac.path_to_ltree("x.mkv", library_id=lib.id),
        )
        s.add(item)
        await s.commit()
        return agent.id, agent.cert_fingerprint, lib.id, item.id


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    # Default (R2 is unconditional): reads are NOT audited.
    monkeypatch.setattr(settings, "audit_reads", False)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def _count(maker, event_type):
    async with maker() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(SecurityEvent)
                .where(SecurityEvent.event_type == event_type)
            )
        ).scalar_one()


async def _verify_events(maker):
    async with maker() as s:
        return (
            (
                await s.execute(
                    select(SecurityEvent).where(
                        SecurityEvent.event_type == "agent_verify_completed"
                    )
                )
            )
            .scalars()
            .all()
        )


async def _enqueue_poll_complete(c, maker, item_id, agent_id, fp, mode, result, *, ok=True):
    """Enqueue a verify command via the operator plane, poll it as the agent, then
    complete it — returning the completion response."""
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": mode})
    assert r.status_code == 200, r.text
    auth = {"Authorization": f"Bearer {fp}"}
    polled = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=auth
    )
    assert polled.status_code == 200, polled.text
    cid = polled.json()[0]["id"]
    done = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": ok, "result": result},
        headers=auth,
    )
    assert done.status_code == 200, done.text
    return done


# --------------------------------------------------------------------------- #
# rehash_check completion audits UNCONDITIONALLY                               #
# --------------------------------------------------------------------------- #
async def test_rehash_completion_audits_with_reads_off(client, maker):
    c, settings = client
    assert settings.audit_reads is False  # the R2 point: audited anyway
    agent_id, fp, _lib, item_id = await _seed(maker)
    # A rehash that reports a drifted content_hash (silent corruption corrected).
    result = {
        "exists": True, "size": 1000,
        "mtime": datetime(2026, 7, 17, 12, 0, tzinfo=UTC).timestamp(),
        "quick_hash": "q0", "content_hash": "cNEW", "content_skipped": False,
    }
    await _enqueue_poll_complete(c, maker, item_id, agent_id, fp, "rehash", result)

    events = await _verify_events(maker)
    assert len(events) == 1
    d = events[0].details
    assert d["kind"] == "rehash_check"
    assert d["item_id"] == str(item_id)
    assert d["agent_id"] == str(agent_id)
    assert d["ok"] is True
    # Outcome carries the hash-mismatch correction.
    assert d["mismatch"] == "changed"
    assert "content_hash" in d["differed"]


async def test_rehash_failed_completion_still_audits(client, maker):
    c, _ = client
    agent_id, fp, _lib, item_id = await _seed(maker)
    # A failed rehash (agent could not read) still records the completion attempt.
    await _enqueue_poll_complete(
        c, maker, item_id, agent_id, fp, "rehash", {"error": "io"}, ok=False
    )
    events = await _verify_events(maker)
    assert len(events) == 1
    assert events[0].details["ok"] is False


# --------------------------------------------------------------------------- #
# stat_check completion does NOT audit (carve-out is rehash-specific)          #
# --------------------------------------------------------------------------- #
async def test_stat_completion_writes_no_verify_audit(client, maker):
    c, _ = client
    agent_id, fp, _lib, item_id = await _seed(maker)
    result = {
        "exists": True, "size": 1000,
        "mtime": datetime(2026, 7, 17, 12, 0, tzinfo=UTC).timestamp(),
    }
    await _enqueue_poll_complete(c, maker, item_id, agent_id, fp, "stat", result)
    assert await _count(maker, "agent_verify_completed") == 0


async def test_idempotent_replay_does_not_double_audit(client, maker):
    c, _ = client
    agent_id, fp, _lib, item_id = await _seed(maker)
    result = {
        "exists": True, "size": 1000,
        "mtime": datetime(2026, 7, 17, 12, 0, tzinfo=UTC).timestamp(),
        "quick_hash": "q0", "content_hash": "c0", "content_skipped": False,
    }
    done = await _enqueue_poll_complete(
        c, maker, item_id, agent_id, fp, "rehash", result
    )
    cid = done.json()["id"]
    auth = {"Authorization": f"Bearer {fp}"}
    # Re-complete the already-``done`` command: idempotent replay, no second audit.
    replay = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": result},
        headers=auth,
    )
    assert replay.status_code == 200
    assert await _count(maker, "agent_verify_completed") == 1
