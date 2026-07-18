"""FIX-11 filesystem-full guardrails + low-space alerting.

Pure policy matrix (GB vs pct floors, conservative-wins), the fail-closed
producer guard (skip-at-critical, warn-continue), the thumbnail generator's guard
propagation, the 5-minutely monitor (alert fire + dedup + recovery + emergency
GC), and the API/banner data contracts (/system/disk, /stats.disk,
jobs_summary.disk).

The DB-backed tests run on the shared pgserver at the migrated ``head`` schema so
they exercise the REAL low-space ``is_system`` alert_rule seeded by
``seed_system_alert_rules`` plus the partial-UNIQUE dedup index.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import diskguard as dg
from filearr.alerts import ops
from filearr.config import get_settings
from filearr.models import AlertChannel, AlertEvent, AlertRuleChannel
from filearr.models import AlertRule as RuleRow

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure policy matrix.                                                          #
# --------------------------------------------------------------------------- #

def _ev(free_gb, total_gb, *, mn=5, wn=20, cp=2.0, wp=10.0):
    return dg.evaluate(
        int(free_gb * dg.GB), int(total_gb * dg.GB),
        min_free_gb=mn, warn_free_gb=wn, crit_pct=cp, warn_pct=wp,
    )


def test_policy_gb_floors():
    assert _ev(3, 200)[0] == dg.CRITICAL      # free < 5 GB
    assert _ev(10, 200)[0] == dg.WARN         # 5 <= free < 20 GB
    assert _ev(50, 200)[0] == dg.OK           # ample


def test_policy_pct_floors():
    # 4 TB volume: GB floors never trip, so the percent axis governs.
    assert _ev(30, 4000)[0] == dg.CRITICAL    # 0.75% < 2%
    assert _ev(300, 4000)[0] == dg.WARN       # 7.5% < 10%
    assert _ev(800, 4000)[0] == dg.OK         # 20%


def test_policy_conservative_axis_wins():
    # 40 GB LXC rootfs, 6 GB free: 15% (pct OK) but < 20 GB (GB WARN) -> WARN.
    status, reason = _ev(6, 40)
    assert status == dg.WARN and "GB floor" in reason
    # 4 TB, 30 GB free: GB axis only WARN, pct axis CRITICAL -> CRITICAL wins.
    status, reason = _ev(30, 4000)
    assert status == dg.CRITICAL and "% floor" in reason


def test_more_severe_ordering():
    assert dg.more_severe(dg.OK, dg.WARN) == dg.WARN
    assert dg.more_severe(dg.CRITICAL, dg.WARN) == dg.CRITICAL
    assert dg.overall_status(
        [{"status": dg.OK}, {"status": dg.WARN}, {"status": dg.OK}]
    ) == dg.WARN


# --------------------------------------------------------------------------- #
# Producer guard (fail-closed).                                               #
# --------------------------------------------------------------------------- #

class _GuardSettings:
    disk_min_free_gb = 5.0
    disk_warn_free_gb = 20.0
    disk_crit_pct_free = 2.0
    disk_warn_pct_free = 10.0
    disk_guard_cache_s = 0.0
    disk_watch_paths: list[str] = []
    disk_pg_path = None
    config_dir = "/tmp/filearr-guard"
    thumbnail_generator_version = 1


def _fake_status(monkeypatch, status):
    dg.clear_cache()
    monkeypatch.setattr(
        dg, "status_for_path",
        lambda path, settings: {
            "path": path, "exists": True, "total": 100 * dg.GB,
            "free": 1 * dg.GB, "used": 99 * dg.GB, "pct_free": 1.0,
            "dev": 1, "status": status, "reason": "test",
        },
    )


def test_guard_write_raises_at_critical(monkeypatch):
    _fake_status(monkeypatch, dg.CRITICAL)
    with pytest.raises(dg.DiskGuardError) as ei:
        dg.guard_write("/tmp/x", _GuardSettings())
    assert "disk_full_guard" in str(ei.value)


def test_guard_write_warn_and_ok_pass(monkeypatch):
    _fake_status(monkeypatch, dg.WARN)
    assert dg.guard_write("/tmp/x", _GuardSettings())["status"] == dg.WARN  # no raise
    _fake_status(monkeypatch, dg.OK)
    assert dg.guard_write("/tmp/x", _GuardSettings())["status"] == dg.OK


def test_guard_fails_open_on_statvfs_error(monkeypatch):
    dg.clear_cache()

    def _boom(path):
        raise OSError("statvfs exploded")

    monkeypatch.setattr(dg, "disk_status", _boom)
    # An unstatable path must NEVER block a writer (monitoring cannot cause an
    # outage): status_for_path returns ok/exists=False and guard_write passes.
    st = dg.guard_write("/tmp/nope", _GuardSettings())
    assert st["status"] == dg.OK and st["exists"] is False


def test_cache_collapses_repeated_checks(monkeypatch):
    dg.clear_cache()
    calls = {"n": 0}

    def _count(path, settings):
        calls["n"] += 1
        return {"path": path, "status": dg.OK, "free": 0, "pct_free": 0.0,
                "reason": "ok", "total": 0, "used": 0, "dev": 1, "exists": True}

    monkeypatch.setattr(dg, "status_for_path", _count)
    s = _GuardSettings()
    s.disk_guard_cache_s = 999.0
    clk = iter([100.0, 100.1, 100.2])
    dg.cached_status_for_path("/p", s, clock=lambda: next(clk))
    dg.cached_status_for_path("/p", s, clock=lambda: next(clk))
    assert calls["n"] == 1  # second call served from cache


async def test_generate_and_store_guard_propagates(monkeypatch, tmp_path):
    """generate_and_store refuses the write (raises DiskGuardError) BEFORE any
    file is created when the cache filesystem is critical."""
    import filearr.tasks.thumbs as thumbs_mod
    from filearr import thumbs as th

    async def _fake_resolve(session, item, tier, settings):
        return "image", th.ThumbBytes(data=b"webpbytes", width=1, height=1)

    monkeypatch.setattr(thumbs_mod, "_resolve_source", _fake_resolve)
    _fake_status(monkeypatch, dg.CRITICAL)

    s = _GuardSettings()
    s.config_dir = str(tmp_path)
    item = SimpleNamespace(content_hash="abcd1234", quick_hash=None, id=uuid4())
    with pytest.raises(dg.DiskGuardError):
        await thumbs_mod.generate_and_store(object(), item, th.TIER_GRID, s)
    # No stray .webp was written under the cache root.
    assert not list(tmp_path.rglob("*.webp"))


# --------------------------------------------------------------------------- #
# DB-backed: monitor alert fire + dedup + recovery + emergency GC.            #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "alert_events", "alert_rule_channels", "alert_rules",
            "alert_channels", "thumbnail_manifest", "items",
            "scan_runs", "libraries",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _enable_low_space_rule(maker):
    """Seed the ops rules, then enable the low-space one + attach a channel."""
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rule = (
            await s.execute(
                select(RuleRow).where(RuleRow.name == ops.LOW_SPACE_RULE_NAME)
            )
        ).scalar_one()
        rule.enabled = True
        ch = AlertChannel(
            name=f"c-{uuid4().hex[:8]}", type_="webhook",
            config={"url": "https://hook.example.com/x"},
            dispatch_locality="central", enabled=True,
        )
        s.add(ch)
        await s.commit()
        s.add(AlertRuleChannel(rule_id=rule.id, channel_id=ch.id))
        await s.commit()
        return rule


def _status(status, path="/config/thumbnails", label="thumbnails", dev=42):
    return {
        "path": path, "label": label, "is_pg": False, "exists": True,
        "total": 100 * dg.GB, "free": 1 * dg.GB, "used": 99 * dg.GB,
        "pct_free": 1.0, "dev": dev, "status": status, "reason": "test floor",
    }


async def test_seed_includes_low_space_rule(maker):
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rows = (
            await s.execute(select(RuleRow).where(RuleRow.is_system.is_(True)))
        ).scalars().all()
    names = sorted(r.name for r in rows)
    assert ops.LOW_SPACE_RULE_NAME in names
    assert all(r.enabled is False for r in rows)  # seeded disabled


async def test_monitor_fires_dedups_and_recovers(maker, monkeypatch):
    from filearr.tasks import diskmon

    await _enable_low_space_rule(maker)
    state: dict = {}
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    # 1. transition ok -> critical fires exactly one alert.
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.CRITICAL)])
    r1 = await diskmon.run_disk_monitor(maker, now=now, state=state)
    assert r1["alerts"] == 1

    async def _count(event_type):
        async with maker() as s:
            return len(
                (
                    await s.execute(
                        select(AlertEvent).where(AlertEvent.event_type == event_type)
                    )
                ).scalars().all()
            )

    assert await _count(ops.LOW_SPACE_EVENT) == 1

    # 2. still critical next tick: no NEW alert (transition + hourly dedup).
    r2 = await diskmon.run_disk_monitor(maker, now=now + timedelta(minutes=5), state=state)
    assert r2["alerts"] == 0
    assert await _count(ops.LOW_SPACE_EVENT) == 1

    # 3. recovery critical -> ok emits a "recovered" clear.
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.OK)])
    r3 = await diskmon.run_disk_monitor(maker, now=now + timedelta(minutes=10), state=state)
    assert r3["recoveries"] == 1
    # recovered row is a distinct dedup key (status-stamped), so total is 2.
    assert await _count(ops.LOW_SPACE_EVENT) == 2


async def test_monitor_warn_to_critical_escalates(maker, monkeypatch):
    from filearr.tasks import diskmon

    await _enable_low_space_rule(maker)
    state: dict = {}
    now = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)

    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.WARN)])
    r1 = await diskmon.run_disk_monitor(maker, now=now, state=state)
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.CRITICAL)])
    r2 = await diskmon.run_disk_monitor(maker, now=now + timedelta(minutes=5), state=state)
    assert r1["alerts"] == 1 and r2["alerts"] == 1  # warn then escalated critical


async def test_monitor_triggers_emergency_gc_at_critical(maker, monkeypatch):
    import filearr.tasks.thumbs as thumbs_mod
    from filearr.tasks import diskmon

    await _enable_low_space_rule(maker)
    calls = []

    async def _spy_gc(*, aggressive=False, target_free_bytes=0):
        calls.append((aggressive, target_free_bytes))
        return {"rows_removed": 0, "files_removed": 0, "bytes_reclaimed": 0, "evicted": 3}

    monkeypatch.setattr(thumbs_mod, "run_thumbnail_gc", _spy_gc)
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.CRITICAL)])
    result = await diskmon.run_disk_monitor(maker, now=datetime.now(UTC), state={})
    assert calls and calls[0][0] is True  # aggressive mode
    assert result["gc"]["evicted"] == 3


async def test_monitor_no_gc_when_ok(maker, monkeypatch):
    import filearr.tasks.thumbs as thumbs_mod
    from filearr.tasks import diskmon

    await _enable_low_space_rule(maker)
    calls = []

    async def _spy_gc(*, aggressive=False, target_free_bytes=0):
        calls.append(1)
        return {}

    monkeypatch.setattr(thumbs_mod, "run_thumbnail_gc", _spy_gc)
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.OK)])
    result = await diskmon.run_disk_monitor(maker, now=datetime.now(UTC), state={})
    assert calls == [] and result["gc"] is None


async def test_emit_low_space_dedup(maker):
    await _enable_low_space_rule(maker)
    now = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    async with maker() as s:
        first = await ops.emit_low_space(
            s, path="/config", label="thumbnails", status="critical",
            free=1, total=100, pct_free=1.0, reason="x", now=now,
        )
    async with maker() as s:
        second = await ops.emit_low_space(
            s, path="/config", label="thumbnails", status="critical",
            free=1, total=100, pct_free=1.0, reason="x", now=now + timedelta(minutes=1),
        )
    assert first is True and second is False  # same path/status/hour collapses


# --------------------------------------------------------------------------- #
# API + banner data contracts.                                               #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def client(pg_uri, monkeypatch):
    from filearr import db as db_mod
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", m)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)

    app = create_app()

    async def _sess():
        async with m() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await engine.dispose()


async def test_system_disk_endpoint_contract(client, monkeypatch):
    monkeypatch.setattr(
        dg, "monitored_statuses",
        lambda s: [_status(dg.WARN, path="/config/thumbnails", label="thumbnails"),
                   _status(dg.OK, path="/tmp", label="tmp", dev=7)],
    )
    r = await client.get("/api/v1/system/disk")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == dg.WARN  # worst of warn+ok
    labels = {p["label"] for p in body["paths"]}
    assert labels == {"thumbnails", "tmp"}
    row = next(p for p in body["paths"] if p["label"] == "thumbnails")
    assert {"path", "free", "total", "pct_free", "status", "reason"} <= set(row)


async def test_stats_has_disk_section(client, monkeypatch):
    monkeypatch.setattr(dg, "monitored_statuses", lambda s: [_status(dg.CRITICAL)])
    r = await client.get("/api/v1/stats")
    assert r.status_code == 200
    disk = r.json()["disk"]
    assert disk["status"] == dg.CRITICAL
    assert disk["paths"][0]["status"] == dg.CRITICAL


async def test_jobs_summary_disk_banner_contract(client, monkeypatch):
    monkeypatch.setattr(
        dg, "monitored_statuses",
        lambda s: [_status(dg.CRITICAL, label="thumbnails"),
                   _status(dg.OK, path="/tmp", label="tmp", dev=7)],
    )
    r = await client.get("/api/v1/system/jobs/summary")
    assert r.status_code == 200
    disk = r.json()["disk"]
    assert disk["status"] == dg.CRITICAL
    # Only the non-ok path is listed for the banner.
    assert [p["label"] for p in disk["low"]] == ["thumbnails"]
