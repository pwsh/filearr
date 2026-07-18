"""P10-T3 — agent stat_check / rehash_check verification flow (central-side).

Covers: the migration head bump (``items.last_verified_at``); the ``POST
/items/{id}/verify`` endpoint (per-mode RBAC matrix, non-agent 404, agents-off
404, 409 dedupe, payload shape); the completion reconcile matrix
(unchanged / changed / deleted / hash-mismatch / content_skipped) including the
tombstone, ``last_verified_at`` stamp, index_sync defer, and mismatch-alert
emission; and the seeded ``System: agent verification mismatch`` rule.

Runs against the migrated pgserver Postgres (mirrors test_agent_commands /
test_rbac_enforcement_p6t4 harnesses).
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import authx, rbac, verify
from filearr import db as db_mod
from filearr import worker as worker_mod
from filearr.alerts import ops
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Agent,
    AgentCommand,
    AlertEvent,
    AlertRule,
    Item,
    ItemStatus,
    Library,
    MediaType,
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
            "agent_commands",
            "alert_events",
            "alert_rules",
            "path_grants",
            "sessions",
            "security_events",
            "items",
            "libraries",
            "users",
            "principals",
            "agents",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker, *, agent_owned=True, mtime=None):
    """Seed an active agent + a library + one active item. When ``agent_owned``
    the library carries ``source_agent_id`` (+ agent_library_ref). Returns
    (agent, library_id, item_id)."""
    mtime = mtime or datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
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
            source_agent_id=agent.id if agent_owned else None,
            agent_library_ref="/data/media" if agent_owned else None,
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            media_type=MediaType.video,
            path="/data/media/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1000,
            mtime=mtime,
            quick_hash="q0",
            content_hash="c0",
            status=ItemStatus.active,
            path_scope=rbac.path_to_ltree("x.mkv", library_id=lib.id),
        )
        s.add(item)
        await s.commit()
        # Detach a lightweight agent stand-in for finalize (name only).
        return types.SimpleNamespace(id=agent.id, name=agent.name), lib.id, item.id


# --------------------------------------------------------------------------- #
# Migration                                                                    #
# --------------------------------------------------------------------------- #
async def test_migration_added_last_verified_at(maker):
    async with maker() as s:
        col = (
            await s.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='items' AND column_name='last_verified_at'"
                )
            )
        ).scalar_one_or_none()
    assert col is not None  # column exists at head (rev b7e3d1f9a2c4)


# --------------------------------------------------------------------------- #
# Seeded alert rule                                                            #
# --------------------------------------------------------------------------- #
async def test_seed_includes_verify_rule(maker):
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rule = (
            await s.execute(
                select(AlertRule).where(AlertRule.name == ops.AGENT_VERIFY_RULE_NAME)
            )
        ).scalar_one()
    assert rule.is_system is True
    assert rule.enabled is False
    assert rule.event_types == [ops.AGENT_VERIFY_MISMATCH_EVENT]


# --------------------------------------------------------------------------- #
# Reconcile matrix (pure-ish: reconcile_completion + finalize)                 #
# --------------------------------------------------------------------------- #
def _cmd(kind, item_id):
    return types.SimpleNamespace(kind=kind, item_id=item_id)


async def _reconcile(maker, agent, cmd, result):
    """Run reconcile_completion (mutates + commit) then finalize; returns the
    outcome and re-loaded item (index_sync defers are captured by the fixture)."""
    now = datetime(2026, 7, 17, 13, 0, tzinfo=UTC)
    async with maker() as s:
        outcome = await verify.reconcile_completion(s, cmd, result, now=now)
        await s.commit()
        if outcome is not None:
            await verify.finalize_completion(s, agent, outcome, now=now)
    async with maker() as s:
        item = await s.get(Item, cmd.item_id)
    return outcome, item


@pytest.fixture
def capture_defer(monkeypatch):
    calls: list[list[str]] = []

    async def _fake(item_ids):
        calls.append(list(item_ids))

    monkeypatch.setattr(worker_mod, "defer_index_sync", _fake)
    return calls


async def _enable_verify_rule(maker):
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rule = (
            await s.execute(
                select(AlertRule).where(AlertRule.name == ops.AGENT_VERIFY_RULE_NAME)
            )
        ).scalar_one()
        rule.enabled = True
        await s.commit()


async def _alert_count(maker):
    async with maker() as s:
        return len(
            (
                await s.execute(
                    select(AlertEvent).where(
                        AlertEvent.event_type == ops.AGENT_VERIFY_MISMATCH_EVENT
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_reconcile_unchanged_stamps_only(maker, capture_defer):
    await _enable_verify_rule(maker)
    agent, _lib, item_id = await _seed(maker)
    # exists, identical size/mtime -> no mismatch, only last_verified_at.
    result = {"exists": True, "size": 1000, "mtime": _seed_mtime()}
    outcome, item = await _reconcile(maker, agent, _cmd("stat_check", item_id), result)
    assert outcome.mismatch is None
    assert item.status == ItemStatus.active
    assert item.last_verified_at is not None
    assert capture_defer == []  # nothing projected changed
    assert await _alert_count(maker) == 0


async def test_reconcile_changed_size_updates_and_alerts(maker, capture_defer):
    await _enable_verify_rule(maker)
    agent, _lib, item_id = await _seed(maker)
    result = {"exists": True, "size": 2048, "mtime": _seed_mtime()}
    outcome, item = await _reconcile(maker, agent, _cmd("stat_check", item_id), result)
    assert outcome.mismatch == "changed" and "size" in outcome.differed
    assert item.size == 2048
    assert item.last_verified_at is not None
    assert capture_defer == [[str(item_id)]]
    assert await _alert_count(maker) == 1


async def test_reconcile_deleted_tombstones_and_alerts(maker, capture_defer):
    await _enable_verify_rule(maker)
    agent, _lib, item_id = await _seed(maker)
    outcome, item = await _reconcile(
        maker, agent, _cmd("stat_check", item_id), {"exists": False}
    )
    assert outcome.mismatch == "deleted" and outcome.tombstoned
    assert item.status == ItemStatus.missing
    assert item.last_verified_at is not None
    assert capture_defer == [[str(item_id)]]
    assert await _alert_count(maker) == 1


async def test_reconcile_hash_mismatch_corrects_content_hash(maker, capture_defer):
    await _enable_verify_rule(maker)
    agent, _lib, item_id = await _seed(maker)
    # rehash: size/mtime identical but content_hash drifted (silent corruption).
    result = {
        "exists": True, "size": 1000, "mtime": _seed_mtime(),
        "quick_hash": "q0", "content_hash": "cNEW", "content_skipped": False,
    }
    outcome, item = await _reconcile(maker, agent, _cmd("rehash_check", item_id), result)
    assert outcome.mismatch == "changed" and "content_hash" in outcome.differed
    assert item.content_hash == "cNEW"
    assert await _alert_count(maker) == 1


async def test_reconcile_content_skipped_is_not_a_mismatch(maker, capture_defer):
    await _enable_verify_rule(maker)
    agent, _lib, item_id = await _seed(maker)
    # oversize rehash: content skipped (null hash) but quick/size/mtime unchanged.
    result = {
        "exists": True, "size": 1000, "mtime": _seed_mtime(),
        "quick_hash": "q0", "content_hash": None, "content_skipped": True,
    }
    outcome, item = await _reconcile(maker, agent, _cmd("rehash_check", item_id), result)
    assert outcome.mismatch is None
    assert item.content_hash == "c0"  # stored hash untouched — null never clears it
    assert item.last_verified_at is not None
    assert capture_defer == []
    assert await _alert_count(maker) == 0


def _seed_mtime() -> float:
    return datetime(2026, 7, 17, 12, 0, tzinfo=UTC).timestamp()


# --------------------------------------------------------------------------- #
# Endpoint — enqueue (auth off, agents enabled)                                #
# --------------------------------------------------------------------------- #
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
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def test_verify_payload_shape_stat_and_rehash(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker)
    # stat -> payload has library_ref + rel_path, NO content.
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "stat_check"
    async with maker() as s:
        cmd = (
            await s.execute(select(AgentCommand).where(AgentCommand.item_id == item_id))
        ).scalar_one()
    assert cmd.payload == {"library_ref": "/data/media", "rel_path": "x.mkv"}
    assert cmd.kind == "stat_check"

    # rehash -> payload adds content: true.
    r2 = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "rehash"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["kind"] == "rehash_check"
    async with maker() as s:
        rows = (
            await s.execute(
                select(AgentCommand).where(
                    AgentCommand.item_id == item_id, AgentCommand.kind == "rehash_check"
                )
            )
        ).scalars().all()
    assert rows[0].payload == {"library_ref": "/data/media", "rel_path": "x.mkv", "content": True}


async def test_verify_404_non_agent_item(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker, agent_owned=False)
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r.status_code == 404


async def test_verify_404_when_agents_disabled(client, maker):
    c, settings = client
    settings.agents_enabled = False
    _agent, _lib, item_id = await _seed(maker)
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r.status_code == 404


async def test_verify_dedupe_409(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker)
    r1 = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r1.status_code == 200
    r2 = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r2.status_code == 409
    # A DIFFERENT mode is a distinct command kind -> not deduped.
    r3 = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "rehash"})
    assert r3.status_code == 200


async def test_verify_bad_mode_422(client, maker):
    c, _ = client
    _agent, _lib, item_id = await _seed(maker)
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "wat"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Endpoint — full complete → reconcile (agent plane)                           #
# --------------------------------------------------------------------------- #
async def test_complete_reconciles_deleted(client, maker):
    c, _ = client
    agent, _lib, item_id = await _seed(maker)
    fp = None
    async with maker() as s:
        fp = (await s.get(Agent, agent.id)).cert_fingerprint
    # enqueue a stat, poll it (agent bearer), then complete exists=false.
    await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    auth = {"Authorization": f"Bearer {fp}"}
    polled = await c.post(f"/api/v1/agents/{agent.id}/commands/poll", json={"max": 5}, headers=auth)
    assert polled.status_code == 200
    cid = polled.json()[0]["id"]
    done = await c.post(
        f"/api/v1/agents/{agent.id}/commands/{cid}/complete",
        json={"ok": True, "result": {"exists": False}},
        headers=auth,
    )
    assert done.status_code == 200
    async with maker() as s:
        item = await s.get(Item, item_id)
    assert item.status == ItemStatus.missing
    assert item.last_verified_at is not None


# --------------------------------------------------------------------------- #
# RBAC matrix (auth on)                                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def rbac_client(maker, monkeypatch):
    from filearr import grant_cache

    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    grant_cache._cache.clear()
    grant_cache.bump_generation()
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def _mk_user(maker, username, role="user"):
    async with maker() as s:
        p = Principal(kind="user", global_role=role)
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username=username.lower(),
                password_hash=authx.hash_password("pw-123456"),
                auth_provider="local",
            )
        )
        await s.commit()
        return p.id


async def _grant(maker, subject_id, library_id, action):
    from filearr import grant_cache, rbac
    from filearr.models import PathGrant

    async with maker() as s:
        s.add(
            PathGrant(
                subject_kind="principal",
                subject_id=subject_id,
                library_id=library_id,
                scope=rbac.library_label(library_id),
                action=action,
                effect="allow",
            )
        )
        await s.commit()
    grant_cache.bump_generation()


async def _login(c, username):
    r = await c.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-123456"}
    )
    assert r.status_code == 200, r.text


async def test_rbac_stat_needs_search_metadata(rbac_client, maker):
    c, _ = rbac_client
    _agent, lib_id, item_id = await _seed(maker)
    uid = await _mk_user(maker, "reader")
    await _grant(maker, uid, lib_id, "search_metadata")
    await _login(c, "reader")
    # stat: search_metadata granted -> allowed.
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "stat"})
    assert r.status_code == 200, r.text
    # rehash: needs download (not granted) but item IS visible -> 403.
    r2 = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "rehash"})
    assert r2.status_code == 403


async def test_rbac_rehash_needs_download(rbac_client, maker):
    c, _ = rbac_client
    _agent, lib_id, item_id = await _seed(maker)
    uid = await _mk_user(maker, "downloader")
    await _grant(maker, uid, lib_id, "search_metadata")
    await _grant(maker, uid, lib_id, "download")
    await _login(c, "downloader")
    r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": "rehash"})
    assert r.status_code == 200, r.text


async def test_rbac_invisible_item_404_both_modes(rbac_client, maker):
    c, _ = rbac_client
    _agent, _lib, item_id = await _seed(maker)
    await _mk_user(maker, "nogrant")  # no grants at all
    await _login(c, "nogrant")
    for mode in ("stat", "rehash"):
        r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": mode})
        assert r.status_code == 404, (mode, r.text)


async def test_rbac_admin_bypass(rbac_client, maker):
    c, _ = rbac_client
    _agent, _lib, item_id = await _seed(maker)
    await _mk_user(maker, "boss", role="admin")
    await _login(c, "boss")
    for mode in ("stat", "rehash"):
        r = await c.post(f"/api/v1/items/{item_id}/verify", json={"mode": mode})
        assert r.status_code == 200, (mode, r.text)
