"""P8-T9/T10/T14 + the dedup unique-index migration — the ops-alert layer.

Everything here runs on the shared pgserver at the migrated ``head`` schema so it
exercises the REAL partial-UNIQUE dedup index (migration ``f3b8d2a41c5e``), the
scan crash handler's alert hook, the pump-driven extract-error-spike detector and
the retention purge task.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.alerts import ops
from filearr.config import get_settings
from filearr.models import (
    AlertChannel,
    AlertEvent,
    AlertRuleChannel,
    Item,
    ItemStatus,
    Library,
)
from filearr.models import AlertRule as RuleRow

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
            "alert_events",
            "alert_rule_channels",
            "alert_rules",
            "alert_channels",
            "items",
            "scan_runs",
            "libraries",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _mk_library(maker, name="lib", root="/nonexistent-root-xyz"):
    async with maker() as s:
        lib = Library(name=name, root_path=root, enabled_categories=[])
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib


async def _mk_system_rule(maker, event_type, *, enabled=True, with_channel=True, **over):
    async with maker() as s:
        rule = RuleRow(
            name=over.pop("name", f"sys-{event_type}-{uuid4().hex[:4]}"),
            enabled=enabled,
            is_system=True,
            event_types=[event_type],
            group_wait_s=0,
            group_by=["event_type", "library_id", "rule_id"],
            threshold_count=over.pop("threshold_count", None),
            threshold_window_s=over.pop("threshold_window_s", None),
        )
        s.add(rule)
        await s.commit()
        await s.refresh(rule)
        if with_channel:
            ch = AlertChannel(
                name=f"c-{uuid4().hex[:8]}",
                type_="webhook",
                config={"url": "https://hook.example.com/x"},
                dispatch_locality="central",
                enabled=True,
            )
            s.add(ch)
            await s.commit()
            s.add(AlertRuleChannel(rule_id=rule.id, channel_id=ch.id))
            await s.commit()
        return rule


async def _add_error_items(maker, lib, n):
    async with maker() as s:
        for _ in range(n):
            s.add(
                Item(
                    library_id=lib.id,
                    file_category="video", file_group="video",
                    status=ItemStatus.active,
                    path=f"/x/{uuid4().hex}",
                    rel_path=f"bad/{uuid4().hex}.mp4",
                    filename="bad.mp4",
                    extension="mp4",
                    size=10,
                    mtime=datetime.now(UTC),
                    metadata_={"_extract_error": "boom"},
                )
            )
        await s.commit()


async def _events(maker, *, event_type=None):
    async with maker() as s:
        stmt = select(AlertEvent)
        if event_type is not None:
            stmt = stmt.where(AlertEvent.event_type == event_type)
        return (await s.execute(stmt)).scalars().all()


async def test_seed_system_rules_idempotent_and_preserving(maker):
    await ops.seed_system_alert_rules(maker)
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rows = (
            await s.execute(select(RuleRow).where(RuleRow.is_system.is_(True)))
        ).scalars().all()
    names = sorted(r.name for r in rows)
    assert names == sorted(
        [
            ops.EXTRACT_SPIKE_RULE_NAME,
            ops.LOW_SPACE_RULE_NAME,
            ops.REPORT_DELIVERY_RULE_NAME,
            ops.SCAN_FAILED_RULE_NAME,
            ops.AGENT_OFFLINE_RULE_NAME,
            ops.AGENT_STALL_RULE_NAME,
            ops.AGENT_VERIFY_RULE_NAME,
        ]
    )
    assert all(r.enabled is False for r in rows)

    async with maker() as s:
        row = (
            await s.execute(
                select(RuleRow).where(RuleRow.name == ops.EXTRACT_SPIKE_RULE_NAME)
            )
        ).scalar_one()
        row.enabled = True
        row.threshold_count = 999
        await s.commit()
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        row = (
            await s.execute(
                select(RuleRow).where(RuleRow.name == ops.EXTRACT_SPIKE_RULE_NAME)
            )
        ).scalar_one()
    assert row.enabled is True and row.threshold_count == 999


async def test_scan_failure_emits_one_event_and_never_masks(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr.tasks import scan as scan_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    lib = await _mk_library(maker, name="crashy")
    await _mk_system_rule(maker, ops.SCAN_FAILED_EVENT, enabled=True)

    with pytest.raises(RuntimeError):
        await scan_mod.scan_library(str(lib.id))

    async with maker() as s:
        run = (await s.execute(text("SELECT status FROM scan_runs LIMIT 1"))).one()
    assert run.status == "failed"

    evs = await _events(maker, event_type=ops.SCAN_FAILED_EVENT)
    assert len(evs) == 1
    assert evs[0].library_id == lib.id
    assert evs[0].item_id is None
    assert "scan failed" in evs[0].payload["message"]
    assert evs[0].payload["run_id"]


async def test_scan_failure_no_event_when_rule_disabled(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr.tasks import scan as scan_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    lib = await _mk_library(maker, name="crashy2")
    await _mk_system_rule(maker, ops.SCAN_FAILED_EVENT, enabled=False)
    with pytest.raises(RuntimeError):
        await scan_mod.scan_library(str(lib.id))
    assert await _events(maker, event_type=ops.SCAN_FAILED_EVENT) == []


async def test_scan_failure_hook_cannot_mask_original_error(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr.tasks import scan as scan_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    async def _boom(*a, **k):
        raise RuntimeError("alert layer exploded")

    monkeypatch.setattr(ops, "emit_scan_failure", _boom)

    lib = await _mk_library(maker, name="crashy3")
    await _mk_system_rule(maker, ops.SCAN_FAILED_EVENT, enabled=True)
    with pytest.raises(Exception) as ei:
        await scan_mod.scan_library(str(lib.id))
    assert "alert layer exploded" not in str(ei.value)


async def test_extract_error_spike_fires_once_per_window_and_ages_out(maker):
    lib = await _mk_library(maker, name="spikey")
    await _mk_system_rule(
        maker,
        ops.EXTRACT_SPIKE_EVENT,
        enabled=True,
        threshold_count=3,
        threshold_window_s=3600,
    )
    state: list = []
    t0 = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)

    await _add_error_items(maker, lib, 2)
    async with maker() as s:
        assert await ops.evaluate_extract_error_spike(s, t0, state=state) == 0
    assert await _events(maker, event_type=ops.EXTRACT_SPIKE_EVENT) == []

    await _add_error_items(maker, lib, 4)
    async with maker() as s:
        got = await ops.evaluate_extract_error_spike(s, t0 + timedelta(minutes=1), state=state)
    assert got == 1
    assert len(await _events(maker, event_type=ops.EXTRACT_SPIKE_EVENT)) == 1

    async with maker() as s:
        got = await ops.evaluate_extract_error_spike(s, t0 + timedelta(minutes=2), state=state)
    assert got == 0
    assert len(await _events(maker, event_type=ops.EXTRACT_SPIKE_EVENT)) == 1

    async with maker() as s:
        got = await ops.evaluate_extract_error_spike(s, t0 + timedelta(hours=2), state=state)
    assert got == 0
    assert len(await _events(maker, event_type=ops.EXTRACT_SPIKE_EVENT)) == 1


async def test_extract_error_spike_below_threshold_silent(maker):
    lib = await _mk_library(maker, name="calm")
    await _mk_system_rule(
        maker, ops.EXTRACT_SPIKE_EVENT, enabled=True, threshold_count=50, threshold_window_s=3600
    )
    state: list = []
    t0 = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
    async with maker() as s:
        await ops.evaluate_extract_error_spike(s, t0, state=state)
    await _add_error_items(maker, lib, 5)
    async with maker() as s:
        got = await ops.evaluate_extract_error_spike(s, t0 + timedelta(minutes=1), state=state)
    assert got == 0
    assert await _events(maker, event_type=ops.EXTRACT_SPIKE_EVENT) == []


async def test_extract_error_spike_noop_when_disabled(maker):
    lib = await _mk_library(maker, name="off")
    await _mk_system_rule(maker, ops.EXTRACT_SPIKE_EVENT, enabled=False, threshold_count=1)
    await _add_error_items(maker, lib, 10)
    async with maker() as s:
        assert await ops.evaluate_extract_error_spike(s, datetime.now(UTC), state=[]) == 0


async def _one_rule(maker):
    async with maker() as s:
        r = RuleRow(
            name="r",
            event_types=["created"],
            group_by=["event_type", "library_id", "rule_id"],
        )
        s.add(r)
        await s.commit()
        await s.refresh(r)
        return r


async def test_dedup_unique_index_blocks_duplicate_pending(maker):
    rule = await _one_rule(maker)
    ev = dict(rule_id=rule.id, event_type="created", dedup_key="k1", payload={})
    async with maker() as s:
        s.add(AlertEvent(**ev))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with maker() as s:
            s.add(AlertEvent(**ev))
            await s.commit()


async def test_dedup_on_conflict_do_nothing_collapses(maker):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rule = await _one_rule(maker)
    vals = dict(rule_id=rule.id, event_type="created", dedup_key="k2", payload={})
    stmt = pg_insert(AlertEvent).values(**vals).on_conflict_do_nothing().returning(AlertEvent.id)
    async with maker() as s:
        assert (await s.execute(stmt)).first() is not None
        await s.commit()
    async with maker() as s:
        assert (await s.execute(stmt)).first() is None
        await s.commit()
    assert len(await _events(maker)) == 1


async def test_dedup_index_is_partial_delivered_frees_key(maker):
    rule = await _one_rule(maker)
    ev = dict(rule_id=rule.id, event_type="created", dedup_key="k3", payload={})
    async with maker() as s:
        row = AlertEvent(**ev)
        s.add(row)
        await s.commit()
        row.delivered = True
        row.delivered_at = datetime.now(UTC)
        await s.commit()
    async with maker() as s:
        s.add(AlertEvent(**ev))
        await s.commit()
    assert len(await _events(maker)) == 2


async def test_retention_purge_terminal_only(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    rule = await _one_rule(maker)
    settings = get_settings()
    old = datetime.now(UTC) - timedelta(days=settings.alert_events_retention_days + 10)
    recent = datetime.now(UTC)
    max_attempts = settings.alert_max_delivery_attempts

    async with maker() as s:
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d1",
                         payload={}, occurred_at=old, delivered=True, delivered_at=old))
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d2",
                         payload={}, occurred_at=old, delivered=False,
                         delivery_attempts=max_attempts, last_error="dead"))
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d3",
                         payload={}, occurred_at=old, delivered=False,
                         delivery_attempts=1))
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d4",
                         payload={}, occurred_at=recent, delivered=True,
                         delivered_at=recent))
        await s.commit()

    deleted = await worker_mod.purge_alert_events(0)
    assert deleted == 2
    remaining = {e.dedup_key for e in await _events(maker)}
    assert remaining == {"d3", "d4"}
