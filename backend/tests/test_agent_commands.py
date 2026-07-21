"""P10-T1 — agent_commands primitive (central-side): migration round-trip, the
pure command state machine (exhaustive transition-matrix property tests) + sweep
decision, the admin API (enqueue/list/get/cancel + FILEARR_AGENTS_ENABLED gate),
the agent plane (poll/ack/complete happy + wrong-agent + replay + size caps), the
TTL/redelivery sweep (bounded), and the enqueue/cancel audit hooks.

Runs against the migrated pgserver Postgres (mirrors test_agents_p5t1's harness).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.agentsync import (
    _COMMAND_TRANSITIONS,
    COMMAND_TERMINAL,
    command_is_terminal,
    command_state_machine,
    run_agent_command_sweep,
    sweep_decision,
)
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentCommand, Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent

_ALL_STATES = ("pending", "picked_up", "done", "failed", "expired", "cancelled")
_ALL_EVENTS = ("deliver", "ack", "complete", "fail", "redeliver", "expire", "cancel")


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure state machine — exhaustive transition-matrix property tests             #
# --------------------------------------------------------------------------- #
def test_state_machine_matrix_is_total_and_deterministic():
    """Every (state, event) pair either maps to a declared next-state or raises;
    a legal transition is deterministic and lands in a valid state; NO transition
    escapes the known state set."""
    for state, event in itertools.product(_ALL_STATES, _ALL_EVENTS):
        if (state, event) in _COMMAND_TRANSITIONS:
            nxt = command_state_machine(state, event)
            assert nxt == command_state_machine(state, event)  # deterministic
            assert nxt in _ALL_STATES
        else:
            with pytest.raises(ValueError):
                command_state_machine(state, event)


def test_terminal_states_are_immutable():
    """No event is legal from any terminal state (done/failed/expired/cancelled)."""
    for state in COMMAND_TERMINAL:
        assert command_is_terminal(state)
        for event in _ALL_EVENTS:
            assert (state, event) not in _COMMAND_TRANSITIONS
            with pytest.raises(ValueError):
                command_state_machine(state, event)


def test_state_machine_happy_path_and_edges():
    assert command_state_machine("pending", "deliver") == "picked_up"
    assert command_state_machine("picked_up", "complete") == "done"
    assert command_state_machine("picked_up", "fail") == "failed"
    assert command_state_machine("picked_up", "ack") == "picked_up"  # lease heartbeat
    assert command_state_machine("picked_up", "redeliver") == "pending"
    assert command_state_machine("pending", "expire") == "expired"
    assert command_state_machine("picked_up", "cancel") == "cancelled"
    # out-of-order: cannot complete before delivery
    with pytest.raises(ValueError):
        command_state_machine("pending", "complete")


def test_sweep_decision_pure():
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    lease, maxa = 300, 5
    kw = dict(now=now, lease_seconds=lease, max_attempts=maxa)
    # terminal -> nothing
    assert (
        sweep_decision(status="done", expires_at=now, picked_up_at=None, attempts=0, **kw) is None
    )
    # pending past TTL -> expire
    assert (
        sweep_decision(
            status="pending",
            expires_at=now - timedelta(seconds=1),
            picked_up_at=None,
            attempts=0,
            **kw,
        )
        == "expire"
    )
    # pending within TTL -> nothing
    assert (
        sweep_decision(
            status="pending",
            expires_at=now + timedelta(hours=1),
            picked_up_at=None,
            attempts=0,
            **kw,
        )
        is None
    )
    # picked_up, unacked past lease, attempts left -> redeliver
    assert (
        sweep_decision(
            status="picked_up",
            expires_at=now + timedelta(hours=1),
            picked_up_at=now - timedelta(seconds=lease + 1),
            attempts=1,
            **kw,
        )
        == "redeliver"
    )
    # picked_up, unacked past lease, attempts exhausted -> exhaust
    assert (
        sweep_decision(
            status="picked_up",
            expires_at=now + timedelta(hours=1),
            picked_up_at=now - timedelta(seconds=lease + 1),
            attempts=5,
            **kw,
        )
        == "exhaust"
    )
    # picked_up within lease -> nothing
    assert (
        sweep_decision(
            status="picked_up",
            expires_at=now + timedelta(hours=1),
            picked_up_at=now - timedelta(seconds=10),
            attempts=1,
            **kw,
        )
        is None
    )
    # expiry OUTRANKS redelivery (past TTL and past lease) -> expire
    assert (
        sweep_decision(
            status="picked_up",
            expires_at=now - timedelta(seconds=1),
            picked_up_at=now - timedelta(seconds=lease + 1),
            attempts=1,
            **kw,
        )
        == "expire"
    )


# --------------------------------------------------------------------------- #
# DB harness                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create an ACTIVE agent (with a bound cert fingerprint) + a library + item.
    Returns (agent_id, item_id, fingerprint)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add_all([agent, lib])
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return agent.id, item.id, fp


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
# Migration round-trip                                                         #
# --------------------------------------------------------------------------- #
async def test_migration_round_trip(db_maker):
    agent_id, item_id, _ = await _seed(db_maker)
    async with db_maker() as s:
        cmd = AgentCommand(
            agent_id=agent_id,
            kind="stat_check",
            item_id=item_id,
            payload={"rel_path": "x.mkv"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        got = (await s.execute(text("SELECT status, attempts FROM agent_commands LIMIT 1"))).one()
        assert got.status == "pending" and got.attempts == 0
    # kind CHECK constraint rejects junk.
    with pytest.raises(Exception):  # noqa: B017
        async with db_maker() as s:
            await s.execute(
                text(
                    "INSERT INTO agent_commands (agent_id,kind,item_id,expires_at) "
                    "VALUES (:a,'wat',:i,now())"
                ).bindparams(a=agent_id, i=item_id)
            )
            await s.commit()
    # status CHECK constraint rejects junk.
    with pytest.raises(Exception):  # noqa: B017
        async with db_maker() as s:
            await s.execute(
                text(
                    "INSERT INTO agent_commands (agent_id,kind,item_id,status,expires_at) "
                    "VALUES (:a,'stat_check',:i,'weird',now())"
                ).bindparams(a=agent_id, i=item_id)
            )
            await s.commit()


# --------------------------------------------------------------------------- #
# Admin API — enqueue / list / get / cancel + gate                             #
# --------------------------------------------------------------------------- #
async def test_enqueue_list_get(client):
    c, maker, _ = client
    agent_id, item_id, _ = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "stat_check", "item_id": str(item_id)},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "pending"

    lst = (await c.get("/api/v1/agent-commands")).json()
    assert len(lst) == 1 and lst[0]["id"] == cid
    # filters
    assert (await c.get("/api/v1/agent-commands", params={"state": "pending"})).json()
    assert (await c.get("/api/v1/agent-commands", params={"state": "done"})).json() == []
    assert (await c.get("/api/v1/agent-commands", params={"kind": "stage_upload"})).json() == []
    one = await c.get(f"/api/v1/agent-commands/{cid}")
    assert one.status_code == 200 and one.json()["agent_id"] == str(agent_id)


async def test_enqueue_unknown_agent_and_item(client):
    c, maker, _ = client
    agent_id, item_id, _ = await _seed(maker)
    # unknown agent
    r = await c.post(
        f"/api/v1/agents/{uuid.uuid4()}/commands",
        json={"kind": "stat_check", "item_id": str(item_id)},
    )
    assert r.status_code == 404
    # unknown item
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "stat_check", "item_id": str(uuid.uuid4())},
    )
    assert r2.status_code == 404
    # bad kind -> 422
    r3 = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "nope", "item_id": str(item_id)},
    )
    assert r3.status_code == 422


async def test_payload_cap(client):
    c, maker, settings = client
    agent_id, item_id, _ = await _seed(maker)
    monkeypatch_val = settings.agent_command_payload_max_bytes
    big = {"blob": "x" * (monkeypatch_val + 100)}
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "stat_check", "item_id": str(item_id), "payload": big},
    )
    assert r.status_code == 413


async def test_cancel_pre_terminal_only(client):
    c, maker, _ = client
    agent_id, item_id, _ = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "stat_check", "item_id": str(item_id)},
        )
    ).json()["id"]
    d = await c.post(f"/api/v1/agent-commands/{cid}/cancel")
    assert d.status_code == 200 and d.json()["status"] == "cancelled"
    # cancelling a terminal command -> 409
    again = await c.post(f"/api/v1/agent-commands/{cid}/cancel")
    assert again.status_code == 409


async def test_feature_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings = client
    agent_id, item_id, _ = await _seed(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.get("/api/v1/agent-commands")).status_code == 404
    assert (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "stat_check", "item_id": str(item_id)},
        )
    ).status_code == 404
    assert (
        await c.post(f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5})
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Agent plane — poll / ack / complete + wrong-agent + replay + caps            #
# --------------------------------------------------------------------------- #
def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


async def test_poll_ack_complete_happy(client):
    c, maker, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "rehash_check", "item_id": str(item_id)},
        )
    ).json()["id"]

    # poll delivers it (pending -> picked_up, attempts++)
    poll = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 10}, headers=_auth(fp)
    )
    assert poll.status_code == 200
    got = poll.json()
    assert len(got) == 1 and got[0]["id"] == cid
    assert got[0]["status"] == "picked_up" and got[0]["attempts"] == 1

    # a second poll returns nothing (already delivered)
    assert (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 10}, headers=_auth(fp)
        )
    ).json() == []

    # ack refreshes lease (stays picked_up)
    ack = await c.post(f"/api/v1/agents/{agent_id}/commands/{cid}/ack", headers=_auth(fp))
    assert ack.status_code == 200 and ack.json()["status"] == "picked_up"

    # complete -> done with result
    done = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"exists": True, "size": 42}},
        headers=_auth(fp),
    )
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert done.json()["result"]["size"] == 42

    # idempotent replay of complete -> still done, no error
    replay = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"exists": True, "size": 999}},
        headers=_auth(fp),
    )
    assert replay.status_code == 200 and replay.json()["status"] == "done"
    assert replay.json()["result"]["size"] == 42  # original result preserved

    # last_seen_at got stamped by the poll
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.last_seen_at is not None


async def test_poll_requires_agent_credential(client):
    c, maker, _ = client
    agent_id, _, fp = await _seed(maker)
    # no bearer
    assert (
        await c.post(f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5})
    ).status_code == 401
    # wrong bearer
    bad = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth("nope")
    )
    assert bad.status_code == 401


async def test_wrong_agent_cannot_ack_or_complete(client):
    c, maker, _ = client
    agent_id, item_id, fp = await _seed(maker)
    other_id, _, other_fp = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "stat_check", "item_id": str(item_id)},
        )
    ).json()["id"]
    await c.post(f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp))

    # agent B authenticates on its OWN url but references agent A's command -> 404
    r = await c.post(
        f"/api/v1/agents/{other_id}/commands/{cid}/complete",
        json={"ok": True},
        headers=_auth(other_fp),
    )
    assert r.status_code == 404
    # agent B tries to auth against agent A's url with B's fingerprint -> 401
    r2 = await c.post(f"/api/v1/agents/{agent_id}/commands/{cid}/ack", headers=_auth(other_fp))
    assert r2.status_code == 401


async def test_result_cap(client):
    c, maker, settings = client
    agent_id, item_id, fp = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "rehash_check", "item_id": str(item_id)},
        )
    ).json()["id"]
    await c.post(f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp))
    big = {"blob": "x" * (settings.agent_command_result_max_bytes + 100)}
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": big},
        headers=_auth(fp),
    )
    assert r.status_code == 413


async def test_ack_non_inflight_conflict(client):
    c, maker, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "stat_check", "item_id": str(item_id)},
        )
    ).json()["id"]
    # not yet polled -> pending -> ack 409
    r = await c.post(f"/api/v1/agents/{agent_id}/commands/{cid}/ack", headers=_auth(fp))
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# TTL / redelivery sweep (bounded)                                             #
# --------------------------------------------------------------------------- #
async def test_sweep_expires_and_redelivers(db_maker):
    agent_id, item_id, _ = await _seed(db_maker)
    now = datetime.now(UTC)
    async with db_maker() as s:
        # (a) pending, past TTL -> expire
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="stat_check",
                item_id=item_id,
                status="pending",
                expires_at=now - timedelta(seconds=1),
            )
        )
        # (b) picked_up, unacked past lease, attempts left -> redeliver
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="stat_check",
                item_id=item_id,
                status="picked_up",
                attempts=1,
                picked_up_at=now - timedelta(seconds=999),
                expires_at=now + timedelta(hours=1),
            )
        )
        # (c) picked_up, unacked past lease, attempts exhausted -> failed
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="stat_check",
                item_id=item_id,
                status="picked_up",
                attempts=5,
                picked_up_at=now - timedelta(seconds=999),
                expires_at=now + timedelta(hours=1),
            )
        )
        # (d) pending, within TTL -> untouched
        s.add(
            AgentCommand(
                agent_id=agent_id,
                kind="stat_check",
                item_id=item_id,
                status="pending",
                expires_at=now + timedelta(hours=1),
            )
        )
        await s.commit()

    async with db_maker() as s:
        counts = await run_agent_command_sweep(
            s, now=datetime.now(UTC), lease_seconds=300, max_attempts=5
        )
    assert counts == {"expired": 1, "redelivered": 1, "exhausted": 1}

    async with db_maker() as s:
        rows = (
            (await s.execute(text("SELECT status FROM agent_commands ORDER BY status")))
            .scalars()
            .all()
        )
    # expired, failed, pending (redelivered), pending (untouched)
    assert sorted(rows) == ["expired", "failed", "pending", "pending"]


async def test_sweep_is_bounded(db_maker):
    agent_id, item_id, _ = await _seed(db_maker)
    now = datetime.now(UTC)
    async with db_maker() as s:
        for _ in range(5):
            s.add(
                AgentCommand(
                    agent_id=agent_id,
                    kind="stat_check",
                    item_id=item_id,
                    status="pending",
                    expires_at=now - timedelta(seconds=1),
                )
            )
        await s.commit()
    async with db_maker() as s:
        counts = await run_agent_command_sweep(
            s, now=datetime.now(UTC), lease_seconds=300, max_attempts=5, limit=2
        )
    assert counts["expired"] == 2  # capped by limit


# --------------------------------------------------------------------------- #
# Audit hooks                                                                  #
# --------------------------------------------------------------------------- #
async def test_audit_events_emitted(client):
    c, maker, _ = client
    agent_id, item_id, _ = await _seed(maker)
    cid = (
        await c.post(
            f"/api/v1/agents/{agent_id}/commands",
            json={"kind": "stat_check", "item_id": str(item_id)},
        )
    ).json()["id"]
    await c.post(f"/api/v1/agent-commands/{cid}/cancel")
    async with maker() as s:
        rows = (await s.execute(text("SELECT event_type FROM security_events"))).scalars().all()
    assert "agent_command_enqueued" in rows
    assert "agent_command_cancelled" in rows
