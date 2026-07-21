"""P8-T5..T8 + P8-T15 — the alerting pipeline end to end.

Two layers, mirroring the move-detection test shape:

* **Real scans on pgserver** (``_scan_body`` against files on disk) assert that an
  active rule persists exactly the right ``alert_events`` for created / modified /
  deleted / moved transitions, that the glob + event-type + hash-change gates
  behave, that a scan with NO rules writes ZERO rows, and that an alert-layer
  failure can never fail the scan.
* **The dispatch pump** (:func:`filearr.tasks.alerts.run_pending_dispatch`) with
  mocked drivers: group-wait batching, retry→cap→terminal, non-retryable
  immediate, digest rollup, the P8-T15 hourly ceiling HOLD, and the R6 locality
  filter. Plus the read-only ``/alert-events`` endpoint shape + cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.alerts import dispatch as alerts_dispatch
from filearr.alerts import pipeline
from filearr.alerts.dispatch import ChannelDeliveryError, DeliveryResult
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    AlertChannel,
    AlertEvent,
    AlertRuleChannel,
    Item,
    Library,
    ScanRun,
)
from filearr.models import (
    AlertRule as RuleRow,
)
from filearr.tasks import alerts as alerts_task

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM alert_events"))
        await conn.execute(text("DELETE FROM alert_rule_channels"))
        await conn.execute(text("DELETE FROM alert_rules"))
        await conn.execute(text("DELETE FROM alert_channels"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

async def _run_scan(session, library):
    from filearr.tasks import scan as scan_mod

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer = scan_mod._defer_extract_batch
    orig_reindex = scan_mod._reindex_library
    scan_mod._defer_extract_batch = _noop_defer
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        return await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex


def _write(path, content=b"AAAA" * 40_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def _hash_all(session, lib):
    from filearr.config import get_settings as gs
    from filearr.sidecar import classify
    from filearr.tasks.extract import full_hash, quick_hash

    full_max = gs().scan_hash_full_max_bytes
    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars().all()
    )
    for r in rows:
        if r.sidecar_of is not None or classify(r.rel_path) is not None:
            continue
        try:
            r.quick_hash = quick_hash(r.path, r.size)
            if r.size is not None and r.size <= full_max:
                r.content_hash = full_hash(r.path)
        except OSError:
            pass
    await session.commit()


async def _mk_library(session, root, name):
    lib = Library(name=name, root_path=str(root), enabled_categories=[])
    session.add(lib)
    await session.commit()
    return lib


async def _mk_rule(session, **over):
    r = RuleRow(
        name=over.pop("name", "r-" + uuid4().hex[:6]),
        event_types=over.pop("event_types", ["created"]),
        path_glob=over.pop("path_glob", None),
        enabled=over.pop("enabled", True),
        is_system=over.pop("is_system", False),
        library_id=over.pop("library_id", None),
        hash_change_only=over.pop("hash_change_only", False),
        group_wait_s=over.pop("group_wait_s", 30),
        digest_window=over.pop("digest_window", None),
        repeat_interval_s=over.pop("repeat_interval_s", None),
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


async def _mk_channel(session, rule, locality="central", enabled=True):
    ch = AlertChannel(
        name="c-" + uuid4().hex[:8],
        type_="webhook",
        config={"url": "https://hook.example.com/x"},
        dispatch_locality=locality,
        enabled=enabled,
    )
    session.add(ch)
    await session.commit()
    session.add(AlertRuleChannel(rule_id=rule.id, channel_id=ch.id))
    await session.commit()
    return ch


async def _events(session, rule=None):
    stmt = select(AlertEvent)
    if rule is not None:
        stmt = stmt.where(AlertEvent.rule_id == rule.id)
    return (await session.execute(stmt)).scalars().all()


class _Driver:
    """A stand-in send_webhook: records calls, optionally raises."""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    async def __call__(self, url, payload, **kw):
        self.calls.append((url, payload, kw))
        if self.exc is not None:
            raise self.exc
        return DeliveryResult(ok=True)


# --------------------------------------------------------------------------- #
# 1. real-scan matching                                                       #
# --------------------------------------------------------------------------- #

async def test_created_glob_and_type_matrix(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "Movie.mkv")
    _write(root / "notes.txt")
    lib = await _mk_library(session, root, "l1")
    await _mk_rule(session, event_types=["created"], path_glob="*.mkv")

    await _run_scan(session, lib)

    evs = await _events(session)
    assert len(evs) == 1
    assert evs[0].event_type == "created"
    # the matched event is the .mkv, not the .txt
    it = (await session.execute(select(Item).where(Item.id == evs[0].item_id))).scalar_one()
    assert it.rel_path == "Movie.mkv"


async def test_no_rules_writes_zero_events(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "a.mkv")
    _write(root / "b.mkv")
    lib = await _mk_library(session, root, "l-none")

    await _run_scan(session, lib)

    assert await _events(session) == []


async def test_deleted_event_captured(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "gone.mkv")
    lib = await _mk_library(session, root, "l-del")
    await _mk_rule(session, event_types=["deleted"], path_glob="*.mkv")

    await _run_scan(session, lib)
    assert await _events(session) == []  # rule only matches 'deleted'

    await _hash_all(session, lib)
    (root / "gone.mkv").unlink()
    await _run_scan(session, lib)

    evs = await _events(session)
    assert len(evs) == 1 and evs[0].event_type == "deleted"


async def test_modified_event_captured(session, tmp_path):
    root = tmp_path / "lib"
    f = root / "clip.mkv"
    _write(f)
    lib = await _mk_library(session, root, "l-mod")
    await _mk_rule(session, event_types=["modified"], path_glob="*.mkv")

    await _run_scan(session, lib)
    assert await _events(session) == []

    await _hash_all(session, lib)
    _write(f, b"BBBB" * 60_000)  # size change -> modified
    await _run_scan(session, lib)

    evs = await _events(session)
    assert len(evs) == 1 and evs[0].event_type == "modified"


async def test_hash_change_only_does_not_fire_at_walk(session, tmp_path):
    root = tmp_path / "lib"
    f = root / "clip.mkv"
    _write(f)
    lib = await _mk_library(session, root, "l-hc")
    # hash_change_only modified rule: new_hash is None at walk time, so the gate
    # (needs BOTH hashes known and differing) correctly does NOT fire.
    await _mk_rule(
        session,
        event_types=["modified"],
        path_glob="*.mkv",
        hash_change_only=True,
    )

    await _run_scan(session, lib)
    await _hash_all(session, lib)
    _write(f, b"CCCC" * 60_000)
    await _run_scan(session, lib)

    assert await _events(session) == []


async def test_moved_event_replaces_created(session, tmp_path):
    root = tmp_path / "lib"
    _write(root / "Old.mkv")
    lib = await _mk_library(session, root, "l-move")
    # two rules: one watches created, one watches moved.
    await _mk_rule(session, name="created-rule", event_types=["created"], path_glob="*.mkv")
    await _mk_rule(session, name="moved-rule", event_types=["moved"], path_glob="*.mkv")

    await _run_scan(session, lib)
    # first scan produced a 'created' for Old.mkv (created-rule)
    first = await _events(session)
    assert {e.event_type for e in first} == {"created"}

    await _hash_all(session, lib)
    (root / "Old.mkv").rename(root / "New.mkv")
    stats = await _run_scan(session, lib)
    assert stats["moved"] == 1

    evs = await _events(session)
    moved = [e for e in evs if e.event_type == "moved"]
    created = [e for e in evs if e.event_type == "created"]
    # exactly one moved event; NO spurious 'created' for the new path this scan.
    assert len(moved) == 1
    created_paths = {(e.payload or {}).get("rel_path") for e in created}
    assert "New.mkv" not in created_paths


async def test_alert_failure_does_not_fail_scan(session, tmp_path, monkeypatch):
    root = tmp_path / "lib"
    _write(root / "a.mkv")
    lib = await _mk_library(session, root, "l-fail")
    await _mk_rule(session, event_types=["created"], path_glob="*.mkv")

    def _boom(*a, **k):
        raise RuntimeError("alert layer exploded")

    # persist_drafts is called inside the scan's wrapped block.
    monkeypatch.setattr(pipeline, "persist_drafts", _boom)

    stats = await _run_scan(session, lib)

    assert stats["seen"] == 1  # scan finished normally
    items = (await session.execute(select(Item).where(Item.library_id == lib.id))).scalars().all()
    assert len(items) == 1  # the item still landed
    assert await _events(session) == []  # alert write failed, swallowed


async def test_dedup_same_event_twice_one_row(session, tmp_path):
    lib = await _mk_library(session, tmp_path / "lib", "l-dedup")
    rule = await _mk_rule(session, event_types=["created"])
    ev = pipeline.FileEvent("created", str(lib.id), "a.mkv")
    from filearr.alerts.rules import AlertRule as RuleDC

    rdc = RuleDC(id=str(rule.id), name=rule.name, event_types=("created",))
    now = datetime.now(UTC)
    d1 = pipeline.build_draft(rdc, ev, item_id=None, now=now)
    d2 = pipeline.build_draft(rdc, ev, item_id=None, now=now)

    assert await pipeline.persist_drafts(session, [d1]) == 1
    await session.commit()
    # same (rule, dedup_key, item_id) while undelivered -> no second row.
    assert await pipeline.persist_drafts(session, [d2]) == 0
    await session.commit()
    assert len(await _events(session, rule)) == 1


# --------------------------------------------------------------------------- #
# 2. dispatch pump (mocked drivers)                                           #
# --------------------------------------------------------------------------- #

async def _seed_group(session, rule, *, n, occurred_at, dedup_key=None, event_type="created"):
    """Seed a group of ``n`` undelivered events sharing a dedup_key.

    Each member gets a DISTINCT ``item_id`` (a real Item under the rule's
    library) — mirroring how the pipeline writes one event per matched file and
    satisfying the P8-T5 dedup partial-UNIQUE index (rule_id, dedup_key,
    item_id). ``item_id`` stays NULL only when the rule has no library scope."""
    dedup_key = dedup_key or ("dk-" + uuid4().hex)
    for i in range(n):
        item_id = None
        if rule.library_id is not None:
            item = Item(
                library_id=rule.library_id,
                file_category="video", file_group="video",
                path=f"/x/{uuid4().hex}",
                rel_path=f"f{i}-{uuid4().hex}.mkv",
                filename=f"f{i}.mkv",
                extension="mkv",
                size=10,
                mtime=occurred_at,
            )
            session.add(item)
            await session.flush()
            item_id = item.id
        session.add(
            AlertEvent(
                rule_id=rule.id,
                item_id=item_id,
                event_type=event_type,
                dedup_key=dedup_key,
                payload={"rel_path": f"f{i}.mkv"},
                occurred_at=occurred_at,
            )
        )
    await session.commit()
    return dedup_key


async def test_group_wait_batching(session, tmp_path, monkeypatch):
    lib = await _mk_library(session, tmp_path / "l", "gw")
    rule = await _mk_rule(session, group_wait_s=30, library_id=lib.id)
    await _mk_channel(session, rule)
    t0 = datetime.now(UTC)
    await _seed_group(session, rule, n=3, occurred_at=t0)

    drv = _Driver()
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    # inside group-wait -> nothing fires
    s1 = await alerts_task.run_pending_dispatch(session, now=t0 + timedelta(seconds=10))
    assert s1["delivered"] == 0 and drv.calls == []

    # past group-wait -> one dispatch covering all three
    s2 = await alerts_task.run_pending_dispatch(session, now=t0 + timedelta(seconds=40))
    assert s2["delivered"] == 1
    assert len(drv.calls) == 1
    assert drv.calls[0][1]["count"] == 3
    rows = await _events(session, rule)
    assert all(r.delivered for r in rows)


async def test_retry_then_cap_terminal(session, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_max_delivery_attempts", 2)
    lib = await _mk_library(session, tmp_path / "l", "retry")
    rule = await _mk_rule(session, group_wait_s=0, library_id=lib.id)
    await _mk_channel(session, rule)
    t0 = datetime.now(UTC)
    await _seed_group(session, rule, n=1, occurred_at=t0)

    drv = _Driver(exc=ChannelDeliveryError("boom", retryable=True))
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    now = t0 + timedelta(seconds=5)
    await alerts_task.run_pending_dispatch(session, now=now)  # attempt 1
    await alerts_task.run_pending_dispatch(session, now=now)  # attempt 2 -> terminal
    # terminal: further pumps do not send
    s3 = await alerts_task.run_pending_dispatch(session, now=now)
    assert len(drv.calls) == 2
    assert s3["delivered"] == 0
    row = (await _events(session, rule))[0]
    assert row.delivered is False
    assert row.delivery_attempts == 2
    assert row.last_error


async def test_non_retryable_immediate_terminal(session, tmp_path, monkeypatch):
    lib = await _mk_library(session, tmp_path / "l", "nr")
    rule = await _mk_rule(session, group_wait_s=0, library_id=lib.id)
    await _mk_channel(session, rule)
    t0 = datetime.now(UTC)
    await _seed_group(session, rule, n=1, occurred_at=t0)

    drv = _Driver(exc=ChannelDeliveryError("nope", retryable=False))
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    now = t0 + timedelta(seconds=5)
    await alerts_task.run_pending_dispatch(session, now=now)
    await alerts_task.run_pending_dispatch(session, now=now)  # already terminal
    assert len(drv.calls) == 1
    row = (await _events(session, rule))[0]
    assert row.delivered is False
    assert row.delivery_attempts >= get_settings().alert_max_delivery_attempts
    assert row.last_error


async def test_digest_rollup_window(session, tmp_path, monkeypatch):
    lib = await _mk_library(session, tmp_path / "l", "dig")
    rule = await _mk_rule(session, digest_window="hourly", library_id=lib.id)
    await _mk_channel(session, rule)
    # occurred inside the 10:00-11:00 bucket
    base = datetime(2026, 7, 12, 10, 15, tzinfo=UTC)
    await _seed_group(session, rule, n=4, occurred_at=base)

    drv = _Driver()
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    # before the boundary -> held
    s1 = await alerts_task.run_pending_dispatch(
        session, now=datetime(2026, 7, 12, 10, 45, tzinfo=UTC)
    )
    assert s1["delivered"] == 0 and drv.calls == []

    # after 11:00 -> one digest of all four
    s2 = await alerts_task.run_pending_dispatch(
        session, now=datetime(2026, 7, 12, 11, 5, tzinfo=UTC)
    )
    assert s2["delivered"] == 1
    assert len(drv.calls) == 1
    assert drv.calls[0][1]["count"] == 4


async def test_ceiling_hold(session, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_rule_max_per_hour", 1)
    lib = await _mk_library(session, tmp_path / "l", "ceil")
    rule = await _mk_rule(session, group_wait_s=0, library_id=lib.id)
    await _mk_channel(session, rule)
    now = datetime.now(UTC)
    # one already-delivered group within the last hour consumes the ceiling
    session.add(
        AlertEvent(
            rule_id=rule.id, event_type="created", dedup_key="already",
            payload={"rel_path": "x"}, occurred_at=now - timedelta(minutes=30),
            delivered=True, delivered_at=now - timedelta(minutes=5),
        )
    )
    await session.commit()
    await _seed_group(
        session, rule, n=2, occurred_at=now - timedelta(seconds=10), dedup_key="pending"
    )

    drv = _Driver()
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    s = await alerts_task.run_pending_dispatch(session, now=now)
    assert s["held"] == 1
    assert drv.calls == []
    held = (await session.execute(
        select(AlertEvent).where(AlertEvent.dedup_key == "pending")
    )).scalars().all()
    assert all(not r.delivered for r in held)
    assert all(r.last_error and "ceiling" in r.last_error for r in held)


async def test_locality_filter_holds_agent_only(session, tmp_path, monkeypatch):
    lib = await _mk_library(session, tmp_path / "l", "loc")
    rule = await _mk_rule(session, group_wait_s=0, library_id=lib.id)
    await _mk_channel(session, rule, locality="agent")  # not central
    t0 = datetime.now(UTC)
    await _seed_group(session, rule, n=1, occurred_at=t0 - timedelta(seconds=5))

    drv = _Driver()
    monkeypatch.setattr(alerts_dispatch, "send_webhook", drv)

    s = await alerts_task.run_pending_dispatch(session, now=t0)
    assert s["delivered"] == 0
    assert drv.calls == []  # agent-locality channel is not dispatched centrally
    assert all(not r.delivered for r in await _events(session, rule))


# --------------------------------------------------------------------------- #
# 3. /alert-events endpoint                                                   #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def client(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM alert_events"))
        await conn.execute(text("DELETE FROM alert_rule_channels"))
        await conn.execute(text("DELETE FROM alert_rules"))
        await conn.execute(text("DELETE FROM alert_channels"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_events_endpoint_shape_and_status(client):
    c, maker = client
    async with maker() as s:
        rule = await _mk_rule(s, event_types=["created"])
        now = datetime.now(UTC)
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="k1",
                         payload={"rel_path": "a"}, occurred_at=now,
                         delivered=True, delivered_at=now))
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="k2",
                         payload={"rel_path": "b"}, occurred_at=now,
                         delivery_attempts=5, last_error="boom\nx"))
        await s.commit()

    r = await c.get("/api/v1/alert-events")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    statuses = {row["status"] for row in rows}
    assert statuses == {"delivered", "failed"}
    failed = next(row for row in rows if row["status"] == "failed")
    assert "\n" not in (failed["last_error"] or "")  # sanitized


async def test_events_endpoint_limit_cap(client):
    c, _ = client
    over = await c.get("/api/v1/alert-events?limit=500")
    assert over.status_code == 422  # capped at 200
    ok = await c.get("/api/v1/alert-events?limit=200")
    assert ok.status_code == 200


async def test_events_endpoint_delivered_filter(client):
    c, maker = client
    async with maker() as s:
        rule = await _mk_rule(s, event_types=["created"])
        now = datetime.now(UTC)
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d1",
                         payload={}, occurred_at=now, delivered=True, delivered_at=now))
        s.add(AlertEvent(rule_id=rule.id, event_type="created", dedup_key="d2",
                         payload={}, occurred_at=now))
        await s.commit()

    only_pending = await c.get("/api/v1/alert-events?delivered=false")
    assert only_pending.status_code == 200
    body = only_pending.json()
    assert len(body) == 1 and body[0]["delivered"] is False
