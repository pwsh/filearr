"""P8-T11 — agent-offline + replication-stall ops alerts.

Runs on the shared pgserver at the migrated ``head`` schema so it exercises the
REAL ``agents`` / ``agent_replication_log`` / ``alert_events`` tables and the P8
dedup partial-UNIQUE index. Mirrors ``test_diskguard_fix11`` (the FIX-11 low-disk
sibling this task was modelled on): the two seeded system rules are enabled with
no channel (dispatch is P8-T6's job; here we assert only that the correct
``alert_events`` rows are produced), and transition state is injected per test so
each tick is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.alerts import ops
from filearr.models import Agent, AgentReplicationLog, AlertEvent
from filearr.models import AlertRule as RuleRow
from filearr.tasks import agentmon

BACKEND_DIR = Path(__file__).resolve().parent.parent

# A fixed evaluation instant so thresholds are exact (48h offline / 6h stall).
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "alert_events",
            "alert_rule_channels",
            "alert_rules",
            "alert_channels",
            "agent_replication_log",
            "agents",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _seed_and_enable(maker):
    """Seed the ops rules and flip the two P8-T11 agent rules to enabled."""
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        await s.execute(
            update(RuleRow)
            .where(
                RuleRow.name.in_(
                    [ops.AGENT_OFFLINE_RULE_NAME, ops.AGENT_STALL_RULE_NAME]
                )
            )
            .values(enabled=True)
        )
        await s.commit()


async def _mk_agent(
    maker,
    *,
    name="nas",
    hostname="nas",
    bound=True,
    revoked=False,
    last_seen_at=None,
    last_reconcile_at=None,
):
    async with maker() as s:
        agent = Agent(
            name=name,
            hostname=hostname,
            platform="linux",
            # unique per-agent fp (partial-unique index) when cert-bound
            cert_fingerprint=(uuid4().hex if bound else None),
            last_seen_at=last_seen_at,
            last_reconcile_at=last_reconcile_at,
            revoked_at=(NOW if revoked else None),
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _add_ledger(maker, agent_id, seq_no, applied_at):
    async with maker() as s:
        s.add(
            AgentReplicationLog(
                agent_id=agent_id, seq_no=seq_no, op="created", applied_at=applied_at
            )
        )
        await s.commit()


async def _events(maker, *, event_type=None):
    async with maker() as s:
        stmt = select(AlertEvent)
        if event_type is not None:
            stmt = stmt.where(AlertEvent.event_type == event_type)
        return (await s.execute(stmt)).scalars().all()


# --- offline ---------------------------------------------------------------


async def test_offline_fires_past_threshold(maker):
    await _seed_and_enable(maker)
    await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=50))  # > 48h
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["offline"] == 1
    evs = await _events(maker, event_type=ops.AGENT_OFFLINE_EVENT)
    assert len(evs) == 1
    assert evs[0].payload["offline_status"] == "offline"


async def test_offline_not_before_threshold(maker):
    await _seed_and_enable(maker)
    await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=47))  # < 48h
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["offline"] == 0
    assert await _events(maker, event_type=ops.AGENT_OFFLINE_EVENT) == []


async def test_offline_not_for_revoked(maker):
    await _seed_and_enable(maker)
    await _mk_agent(maker, revoked=True, last_seen_at=NOW - timedelta(hours=50))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["evaluated"] == 0
    assert res["offline"] == 0


async def test_offline_not_for_pending_unbound(maker):
    await _seed_and_enable(maker)
    # cert not bound (pending enrollment) — even if long-silent, never pages.
    await _mk_agent(maker, bound=False, last_seen_at=NOW - timedelta(hours=50))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["evaluated"] == 0
    assert res["offline"] == 0


async def test_offline_never_seen_does_not_fire(maker):
    # A cert-bound agent that never checked in (last_seen_at NULL) is treated like
    # a fresh enrollee — no meaningful "offline for Xh", offline is normal.
    await _seed_and_enable(maker)
    await _mk_agent(maker, last_seen_at=None)
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["offline"] == 0


async def test_offline_recovery_clears_on_reappearance(maker):
    await _seed_and_enable(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=50))
    state: dict = {}
    r1 = await agentmon.run_agent_monitor(maker, now=NOW, state=state)
    assert r1["offline"] == 1
    # agent checks back in
    async with maker() as s:
        await s.execute(
            update(Agent).where(Agent.id == agent.id).values(last_seen_at=NOW)
        )
        await s.commit()
    r2 = await agentmon.run_agent_monitor(
        maker, now=NOW + timedelta(minutes=5), state=state
    )
    assert r2["offline_recovered"] == 1
    recs = [
        e
        for e in await _events(maker, event_type=ops.AGENT_OFFLINE_EVENT)
        if e.payload["offline_status"] == "recovered"
    ]
    assert len(recs) == 1


async def test_offline_dedup_two_ticks_one_event(maker):
    await _seed_and_enable(maker)
    await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=50))
    # Two ticks with FRESH state each (simulates a worker restart clearing the
    # transition dict): the hourly-window dedup key + ON CONFLICT still collapses.
    await agentmon.run_agent_monitor(maker, now=NOW, state={})
    await agentmon.run_agent_monitor(maker, now=NOW + timedelta(minutes=5), state={})
    assert len(await _events(maker, event_type=ops.AGENT_OFFLINE_EVENT)) == 1


# --- replication stall -----------------------------------------------------


async def test_stall_fires_for_alive_but_silent(maker):
    await _seed_and_enable(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=1))  # alive
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=7))  # > 6h
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["stalled"] == 1
    assert res["offline"] == 0
    evs = await _events(maker, event_type=ops.AGENT_STALL_EVENT)
    assert len(evs) == 1
    assert evs[0].payload["stall_status"] == "stalled"


async def test_stall_not_for_fresh_enrollee(maker):
    # Alive, but zero ledger rows AND last_reconcile_at NULL: never replicated,
    # cannot have stalled.
    await _seed_and_enable(maker)
    await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=1))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["stalled"] == 0
    assert await _events(maker, event_type=ops.AGENT_STALL_EVENT) == []


async def test_stall_not_when_reconcile_recent(maker):
    # Ledger applied long ago, but a recent reconcile advances the watermark.
    await _seed_and_enable(maker)
    agent = await _mk_agent(
        maker,
        last_seen_at=NOW - timedelta(hours=1),
        last_reconcile_at=NOW - timedelta(hours=1),
    )
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=7))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["stalled"] == 0


async def test_stall_not_when_offline(maker):
    # A long-offline agent whose ledger is also old must NOT also fire stall
    # (offline is the softer signal; no double-alert).
    await _seed_and_enable(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=50))
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=40))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["offline"] == 1
    assert res["stalled"] == 0


async def test_stall_recovery_on_new_ledger_row(maker):
    await _seed_and_enable(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=1))
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=7))
    state: dict = {}
    r1 = await agentmon.run_agent_monitor(maker, now=NOW, state=state)
    assert r1["stalled"] == 1
    # a new outbox entry applies
    await _add_ledger(maker, agent.id, 2, NOW + timedelta(minutes=1))
    r2 = await agentmon.run_agent_monitor(
        maker, now=NOW + timedelta(minutes=5), state=state
    )
    assert r2["stall_recovered"] == 1
    recs = [
        e
        for e in await _events(maker, event_type=ops.AGENT_STALL_EVENT)
        if e.payload["stall_status"] == "recovered"
    ]
    assert len(recs) == 1


async def test_stall_dedup_two_ticks_one_event(maker):
    await _seed_and_enable(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=1))
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=7))
    await agentmon.run_agent_monitor(maker, now=NOW, state={})
    await agentmon.run_agent_monitor(maker, now=NOW + timedelta(minutes=5), state={})
    assert len(await _events(maker, event_type=ops.AGENT_STALL_EVENT)) == 1


# --- rule gating -----------------------------------------------------------


async def test_no_events_when_rules_disabled(maker):
    # Seeded but NOT enabled: the emit helpers no-op.
    await ops.seed_system_alert_rules(maker)
    agent = await _mk_agent(maker, last_seen_at=NOW - timedelta(hours=50))
    await _add_ledger(maker, agent.id, 1, NOW - timedelta(hours=7))
    res = await agentmon.run_agent_monitor(maker, now=NOW, state={})
    assert res["offline"] == 0 and res["stalled"] == 0
    assert await _events(maker) == []


# --- feature gates ---------------------------------------------------------


async def test_agents_disabled_is_noop():
    # Default FILEARR_AGENTS_ENABLED=false: the periodic task returns skipped
    # without touching the DB.
    res = await agentmon.monitor_agents(int(NOW.timestamp()))
    assert res["skipped"] == "agents disabled"
    assert res["offline"] == 0 and res["stalled"] == 0


async def test_tables_absent_is_noop(pg_uri):
    # to_regclass('agents') resolves via search_path; pin an EMPTY schema so the
    # table appears absent — the guard must return zeros (totality on bare DBs).
    engine = create_async_engine(
        _psycopg3(pg_uri),
        connect_args={"options": "-csearch_path=agentmon_empty_p8t11"},
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS agentmon_empty_p8t11"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        res = await agentmon.run_agent_monitor(factory, now=NOW, state={})
        assert res == {
            "evaluated": 0,
            "offline": 0,
            "offline_recovered": 0,
            "stalled": 0,
            "stall_recovered": 0,
        }
    finally:
        await engine.dispose()
