"""P5-T4 — central-side replication apply path.

Covers the pure-ish transactional core (:func:`filearr.agentsync.apply_batch`)
against the migrated Postgres, and the ``POST /agents/{id}/replication-batch``
endpoint (gate / auth / body-agent mismatch / entries cap / 409 seq-gap / 200
happy path / last_seen refresh / index-sync defer-after-commit), plus the
agent-owned-library scan guards (scheduler exclusion + manual-trigger 422).

Mirrors the test_agent_commands harness (migrated pgserver Postgres).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.agentsync import AgentEvent, ReplicationBatch, apply_batch
from filearr.api import agent_commands as agent_commands_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, Item, ItemStatus, Library

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
        await conn.execute(text("DELETE FROM agent_replication_log"))
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_agent(maker, *, name="nas", hostname="nas", seq=0) -> tuple[uuid.UUID, str]:
    """Create an ACTIVE agent (bound cert fingerprint). Returns (agent_id, fp)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name=name,
            hostname=hostname,
            platform="linux",
            cert_fingerprint=fp,
            last_contiguous_seq_no=seq,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _ev(seq, etype, rel_path, *, library_ref="/srv/media", from_rel_path=None,
        size=100, mtime=1_700_000_000.0, quick_hash="q", content_hash=None):
    return AgentEvent(
        seq_no=seq,
        event_type=etype,
        library_ref=library_ref,
        rel_path=rel_path,
        from_rel_path=from_rel_path,
        size=size,
        mtime=mtime,
        quick_hash=quick_hash,
        content_hash=content_hash,
    )


def _batch(agent_id, *events):
    return ReplicationBatch(agent_id=str(agent_id), entries=list(events))


async def _apply(maker, agent_id, batch):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await apply_batch(s, agent, batch)


# --------------------------------------------------------------------------- #
# apply_batch — create / update / tombstone / moved                            #
# --------------------------------------------------------------------------- #
async def test_create_stamps_identity_media_type_scope(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    res = await _apply(
        db_maker, agent_id, _batch(agent_id, _ev(1, "created", "movies/x.mkv"))
    )
    assert res["applied"] == 1 and res["upserted"] == 1 and res["tombstoned"] == 0
    assert res["libraries_created"] == 1 and res["last_seq"] == 1

    async with db_maker() as s:
        item = (
            await s.execute(text("SELECT * FROM items"))
        ).mappings().one()
        assert item["rel_path"] == "movies/x.mkv"
        assert item["file_category"] == "video"          # media_types.detect
        # W8-A: apply_batch stamps the DB-backed taxonomy (category, group) too.
        assert item["file_category"] == "video"
        assert item["file_group"] == "video"
        assert item["status"] == "active"
        assert str(item["source_agent_id"]) == str(agent_id)  # ruling R2 stamp
        assert item["path_scope"] is not None          # rbac.path_to_ltree stamped
        assert item["quick_hash"] == "q"
        # mtime float epoch -> UTC datetime (scan.py convention)
        assert item["mtime"].astimezone(UTC) == datetime.fromtimestamp(
            1_700_000_000.0, tz=UTC
        )
        # library auto-provisioned, name "<agent>: <basename>", root = library_ref
        lib = (await s.execute(text("SELECT * FROM libraries"))).mappings().one()
        assert lib["name"] == "nas: media"
        assert lib["root_path"] == "/srv/media"
        assert str(lib["source_agent_id"]) == str(agent_id)
        assert lib["agent_library_ref"] == "/srv/media"


async def test_update_existing_row(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "created", "a.mkv")))
    res = await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev(2, "modified", "a.mkv", size=222, quick_hash="q2")),
    )
    assert res["upserted"] == 1 and res["last_seq"] == 2
    async with db_maker() as s:
        cnt = (await s.execute(text("SELECT count(*) FROM items"))).scalar_one()
        assert cnt == 1  # updated in place, not duplicated
        row = (await s.execute(text("SELECT size, quick_hash FROM items"))).one()
        assert row.size == 222 and row.quick_hash == "q2"


async def test_tombstone_existing_and_moved_pair(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "created", "a.mkv")))
    # delete a.mkv
    res = await _apply(db_maker, agent_id, _batch(agent_id, _ev(2, "deleted", "a.mkv")))
    assert res["tombstoned"] == 1 and res["noop_tombstones"] == 0
    async with db_maker() as s:
        st = (await s.execute(text("SELECT status FROM items"))).scalar_one()
        assert st == "missing"  # tombstone, never hard-delete (invariant 4)

    # moved: old exists -> tombstoned; new -> created
    await _apply(db_maker, agent_id, _batch(agent_id, _ev(3, "created", "b.mkv")))
    res2 = await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev(4, "moved", "c.mkv", from_rel_path="b.mkv")),
    )
    assert res2["upserted"] == 1 and res2["tombstoned"] == 1
    async with db_maker() as s:
        rows = dict(
            (r.rel_path, r.status)
            for r in (await s.execute(text("SELECT rel_path, status FROM items"))).all()
        )
        assert rows["b.mkv"] == "missing" and rows["c.mkv"] == "active"


async def test_r2_noop_tombstone_counter(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    # delete a never-seen path -> R2 counted no-op (library still provisioned)
    res = await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "deleted", "ghost.mkv")))
    assert res["tombstoned"] == 0 and res["noop_tombstones"] == 1
    assert res["applied"] == 1 and res["last_seq"] == 1
    async with db_maker() as s:
        assert (await s.execute(text("SELECT count(*) FROM items"))).scalar_one() == 0


async def test_collapse_last_event_wins_recreate_over_move(db_maker):
    """Ordering + collapse: within one batch a moved x->y then a re-create of x
    leaves x active (last-event-per-path wins), y created, nothing tombstoned."""
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "created", "x.mkv")))
    res = await _apply(
        db_maker,
        agent_id,
        _batch(
            agent_id,
            _ev(2, "moved", "y.mkv", from_rel_path="x.mkv"),
            _ev(3, "created", "x.mkv"),
        ),
    )
    assert res["tombstoned"] == 0  # the move's delete of x was overridden
    async with db_maker() as s:
        rows = dict(
            (r.rel_path, r.status)
            for r in (await s.execute(text("SELECT rel_path, status FROM items"))).all()
        )
        assert rows["x.mkv"] == "active" and rows["y.mkv"] == "active"


# --------------------------------------------------------------------------- #
# Ledger + idempotency                                                         #
# --------------------------------------------------------------------------- #
async def test_ledger_row_per_entry_and_watermark(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(
        db_maker,
        agent_id,
        _batch(
            agent_id,
            _ev(1, "created", "a"),
            _ev(2, "created", "b"),
            _ev(3, "deleted", "ghost"),
        ),
    )
    async with db_maker() as s:
        led = (
            await s.execute(
                text("SELECT seq_no, op FROM agent_replication_log ORDER BY seq_no")
            )
        ).all()
        assert [(r.seq_no, r.op) for r in led] == [
            (1, "created"),
            (2, "created"),
            (3, "deleted"),
        ]
        wm = (await s.execute(text("SELECT last_contiguous_seq_no FROM agents"))).scalar_one()
        assert wm == 3


async def test_idempotent_replay_no_duplicate_ledger_rows(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    batch = _batch(agent_id, _ev(1, "created", "a"), _ev(2, "created", "b"))
    await _apply(db_maker, agent_id, batch)
    # Replay the SAME batch (ON CONFLICT DO NOTHING backstop). Re-load a FRESH
    # agent row so last_contiguous_seq_no reflects the committed advance.
    res2 = await _apply(db_maker, agent_id, batch)
    assert res2["last_seq"] == 2
    async with db_maker() as s:
        led = (await s.execute(text("SELECT count(*) FROM agent_replication_log"))).scalar_one()
        items = (await s.execute(text("SELECT count(*) FROM items"))).scalar_one()
        libs = (await s.execute(text("SELECT count(*) FROM libraries"))).scalar_one()
        assert led == 2 and items == 2 and libs == 1  # no duplicates


# --------------------------------------------------------------------------- #
# Auto-library creation: name collision suffix + reuse                         #
# --------------------------------------------------------------------------- #
async def test_library_name_collision_suffix_and_reuse(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    # Pre-existing library named exactly "nas: media" forces the (2) suffix.
    async with db_maker() as s:
        s.add(Library(name="nas: media", root_path="/unrelated"))
        await s.commit()

    res1 = await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "created", "x")))
    assert res1["libraries_created"] == 1
    # Second batch for the SAME library_ref reuses the row (no new library).
    res2 = await _apply(db_maker, agent_id, _batch(agent_id, _ev(2, "created", "y")))
    assert res2["libraries_created"] == 0
    async with db_maker() as s:
        names = sorted(
            r[0] for r in (await s.execute(text("SELECT name FROM libraries"))).all()
        )
        assert names == ["nas: media", "nas: media (2)"]
        owned = (
            await s.execute(
                text("SELECT count(*) FROM libraries WHERE source_agent_id IS NOT NULL")
            )
        ).scalar_one()
        assert owned == 1  # reused, not duplicated


# --------------------------------------------------------------------------- #
# Endpoint                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)

    # Record index-sync defers (mock the defer like sibling tests do). Also proves
    # defer-after-commit: capture the item ids and whether they are committed.
    calls: list[list[str]] = []

    async def _fake_defer(item_ids):
        calls.append(list(item_ids))

    monkeypatch.setattr(agent_commands_mod, "defer_index_sync", _fake_defer)

    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings, calls
    app.dependency_overrides.clear()


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


def _body(agent_id, *events) -> dict:
    return _batch(agent_id, *events).model_dump()


async def test_endpoint_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings, _ = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, _ev(1, "created", "a")),
        headers=_auth(fp),
    )
    assert r.status_code == 404


async def test_endpoint_auth(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)
    other_id, other_fp = await _seed_agent(maker, name="other")
    # missing bearer -> 401
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, _ev(1, "created", "a")),
    )
    assert r.status_code == 401
    # wrong bearer -> 401
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, _ev(1, "created", "a")),
        headers=_auth("nope"),
    )
    assert r2.status_code == 401
    # body agent_id != path agent_id -> 403 (authenticated as path agent)
    r3 = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(other_id, _ev(1, "created", "a")),
        headers=_auth(fp),
    )
    assert r3.status_code == 403


async def test_endpoint_seq_gap_409(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)  # last_contiguous_seq_no = 0
    # start at seq 5 -> gap, expected 1
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, _ev(5, "created", "a")),
        headers=_auth(fp),
    )
    assert r.status_code == 409
    assert r.json() == {"reason": "gap", "expected_seq_no": 1}
    # 409 path still refreshes last_seen_at (agent is alive)
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.last_seen_at is not None


async def test_endpoint_happy_path_and_defer_after_commit(client):
    c, maker, _, calls = client
    agent_id, fp = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, _ev(1, "created", "a.mkv"), _ev(2, "created", "b.mkv")),
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "applied": 2,
        "upserted": 2,
        "tombstoned": 0,
        "noop_tombstones": 0,
        "libraries_created": 1,
        "last_seq": 2,
    }
    # index_sync defer was called with the two touched item ids, AFTER commit:
    # the ids must be resolvable as committed rows in a fresh session.
    assert len(calls) == 1 and len(calls[0]) == 2
    async with maker() as s:
        for iid in calls[0]:
            got = await s.get(Item, uuid.UUID(iid))
            assert got is not None and got.status == ItemStatus.active
        a = await s.get(Agent, agent_id)
        assert a.last_seen_at is not None and a.last_contiguous_seq_no == 2


async def test_endpoint_entries_cap_413(client, monkeypatch):
    c, maker, settings, _ = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agent_replication_max_entries", 2)
    events = [_ev(i, "created", f"f{i}") for i in range(1, 4)]  # 3 > cap 2
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json=_body(agent_id, *events),
        headers=_auth(fp),
    )
    assert r.status_code == 413


# --------------------------------------------------------------------------- #
# Agent-owned library scan guards                                              #
# --------------------------------------------------------------------------- #
async def test_scheduler_excludes_agent_owned_library(db_maker, monkeypatch):
    """An agent-owned library is never selected for a due cron scan."""
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", db_maker)
    agent_id, _ = await _seed_agent(db_maker)
    async with db_maker() as s:
        # every-minute cron, but agent-owned -> must be excluded from selection
        s.add(
            Library(
                name="agent-lib",
                root_path="/srv/media",
                scan_cron="* * * * *",
                source_agent_id=agent_id,
                agent_library_ref="/srv/media",
            )
        )
        await s.commit()

    deferred_calls: list = []

    async def _fake_defer_scan(library_id, *, rel_path=None, **k):
        deferred_calls.append((library_id, rel_path))
        return "job"

    monkeypatch.setattr(worker_mod, "defer_scan", _fake_defer_scan)
    deferred = await worker_mod._defer_due_scans(datetime.now(UTC))
    assert deferred == [] and deferred_calls == []


async def test_manual_scan_trigger_422_on_agent_owned(client):
    c, maker, _, _ = client
    agent_id, _ = await _seed_agent(maker)
    async with maker() as s:
        lib = Library(
            name="agent-lib",
            root_path="/srv/media",
            source_agent_id=agent_id,
            agent_library_ref="/srv/media",
        )
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    r = await c.post(f"/api/v1/libraries/{lib_id}/scan")
    assert r.status_code == 422
    assert "agent" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# P10-T11 — additive share_hint round-trips into apply_batch                    #
# --------------------------------------------------------------------------- #
_HINT = {
    "share_url": "smb://nas/media/movies/x.mkv",
    "unc": r"\\nas\media\movies\x.mkv",
    "share_name": "media",
    "host": "nas",
    "source": "agent",
}


def _ev_hint(seq, etype, rel_path, hint, **kw):
    """An AgentEvent carrying the additive share_hint (P10-T11)."""
    ev = _ev(seq, etype, rel_path, **kw)
    return ev.model_copy(update={"share_hint": hint})


async def test_share_hint_stored_on_create(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev_hint(1, "created", "movies/x.mkv", _HINT)),
    )
    async with db_maker() as s:
        item = (await s.execute(text("SELECT share_hint FROM items"))).mappings().one()
        assert item["share_hint"] == _HINT  # stored verbatim as JSONB


async def test_share_hint_absent_is_null(db_maker):
    """A created event WITHOUT a hint (the normal R1 case) stores NULL."""
    agent_id, _ = await _seed_agent(db_maker)
    await _apply(db_maker, agent_id, _batch(agent_id, _ev(1, "created", "movies/x.mkv")))
    async with db_maker() as s:
        item = (await s.execute(text("SELECT share_hint FROM items"))).mappings().one()
        assert item["share_hint"] is None


async def test_share_hint_refreshed_but_hintless_event_does_not_clobber(db_maker):
    """A later hint refreshes; a hint-LESS modified event keeps the prior hint."""
    agent_id, _ = await _seed_agent(db_maker)
    # create with an initial hint
    await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev_hint(1, "created", "movies/x.mkv", _HINT)),
    )
    # modified WITH a new hint -> refreshes
    new_hint = {**_HINT, "share_url": "smb://nas/media/movies/x-v2.mkv"}
    await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev_hint(2, "modified", "movies/x.mkv", new_hint)),
    )
    async with db_maker() as s:
        got = (await s.execute(text("SELECT share_hint FROM items"))).scalar_one()
        assert got["share_url"] == "smb://nas/media/movies/x-v2.mkv"
    # modified WITHOUT a hint -> the prior hint is preserved (not clobbered to NULL)
    await _apply(
        db_maker,
        agent_id,
        _batch(agent_id, _ev(3, "modified", "movies/x.mkv", size=200)),
    )
    async with db_maker() as s:
        got = (await s.execute(text("SELECT share_hint FROM items"))).scalar_one()
        assert got is not None and got["share_url"] == "smb://nas/media/movies/x-v2.mkv"
