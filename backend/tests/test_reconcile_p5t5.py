"""P5-T5 — central-side full-manifest reconciliation sweep.

Covers the µs-canonicalized digest contract, the transactional anti-join core
(:func:`filearr.agentsync.reconcile_start` / ``reconcile_stage_rows`` /
``reconcile_finish``) against migrated Postgres, the three reconcile endpoints
(gate / auth / page cap / TTL expiry / superseding session / digest-mismatch 409
/ happy-path counters / index-sync defer-after-commit), and the §4.5 purge-safety
watermark gate in ``worker.purge_recycle_bin``.

Mirrors the test_replication_p5t4 harness (migrated Postgres via alembic head).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import agentsync
from filearr import db as db_mod
from filearr import worker as worker_mod
from filearr.agentsync import ManifestRow, manifest_digest
from filearr.api import agent_commands as agent_commands_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.media_types import detect
from filearr.models import (
    Agent,
    AgentReconcileSession,
    AgentReconcileStaging,
    Item,
    ItemStatus,
    Library,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

T0 = 1_700_000_000.0  # a clean epoch-seconds mtime used throughout


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
        await conn.execute(text("DELETE FROM agent_reconcile_staging"))
        await conn.execute(text("DELETE FROM agent_reconcile_sessions"))
        await conn.execute(text("DELETE FROM agent_replication_log"))
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_agent(
    maker, *, name="nas", seq=0, revoked=False, last_reconcile_at=None
) -> tuple[uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name=name,
            hostname=name,
            platform="linux",
            cert_fingerprint=fp,
            last_contiguous_seq_no=seq,
            revoked_at=datetime.now(UTC) if revoked else None,
            last_reconcile_at=last_reconcile_at,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


async def _seed_library(maker, agent_id, *, ref="/srv/media", name="agent-lib") -> uuid.UUID:
    async with maker() as s:
        lib = Library(
            name=name, root_path=ref, source_agent_id=agent_id, agent_library_ref=ref
        )
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(
    maker,
    lib_id,
    rel_path,
    *,
    status=ItemStatus.active,
    size=100,
    mtime=T0,
    quick_hash="q",
    content_hash=None,
    source_agent_id=None,
    deleted_at=None,
) -> uuid.UUID:
    async with maker() as s:
        it = Item(
            library_id=lib_id,
            media_type=detect(rel_path),
            path=rel_path,
            rel_path=rel_path,
            filename=rel_path.replace("\\", "/").rsplit("/", 1)[-1],
            size=size,
            mtime=datetime.fromtimestamp(mtime, tz=UTC),
            quick_hash=quick_hash,
            content_hash=content_hash,
            status=status,
            source_agent_id=source_agent_id,
            deleted_at=deleted_at,
        )
        s.add(it)
        await s.commit()
        return it.id


def _mrow(rel_path, *, size=100, mtime=T0, quick_hash="q", content_hash=None) -> ManifestRow:
    return ManifestRow(
        rel_path=rel_path,
        size=size,
        mtime=mtime,
        quick_hash=quick_hash,
        content_hash=content_hash,
    )


# --- direct-call helpers (each reconcile_* fn opens+commits its own txn) ------
async def _start(maker, agent_id, ref, digest, row_count, *, rebuilt=False, ttl=3600):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await agentsync.reconcile_start(
            s, agent, library_ref=ref, digest=digest, row_count=row_count,
            rebuilt=rebuilt, now=datetime.now(UTC), ttl_seconds=ttl,
        )


async def _rows(maker, agent_id, session_id, rows, *, ttl=3600):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await agentsync.reconcile_stage_rows(
            s, agent, session_id=session_id, rows=rows,
            now=datetime.now(UTC), ttl_seconds=ttl,
        )


async def _finish(maker, agent_id, session_id, digest, row_count, *, reset_seq=False, ttl=3600):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await agentsync.reconcile_finish(
            s, agent, session_id=session_id, digest=digest, row_count=row_count,
            reset_seq=reset_seq, now=datetime.now(UTC), ttl_seconds=ttl,
        )


async def _statuses(maker, lib_id) -> dict[str, str]:
    async with maker() as s:
        rows = (
            await s.execute(
                select(Item.rel_path, Item.status).where(Item.library_id == lib_id)
            )
        ).all()
        return {r.rel_path: r.status.value for r in rows}


# --------------------------------------------------------------------------- #
# reconcile_start — the digest match fast path                                 #
# --------------------------------------------------------------------------- #
async def test_start_match_stamps_watermark(db_maker):
    agent_id, _ = await _seed_agent(db_maker, seq=5)
    lib_id = await _seed_library(db_maker, agent_id)
    await _mk_item(db_maker, lib_id, "a.mkv")
    await _mk_item(db_maker, lib_id, "b.mkv", size=200)
    # The agent's digest over the SAME active corpus -> match.
    digest = manifest_digest([_mrow("a.mkv"), _mrow("b.mkv", size=200)])
    res = await _start(db_maker, agent_id, "/srv/media", digest, 2)
    assert res == {"status": "match"}
    async with db_maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.last_reconcile_at is not None
        assert a.last_contiguous_seq_no == 5  # not rebuilt -> unchanged
        # no session created on a match
        n = (await s.execute(text("SELECT count(*) FROM agent_reconcile_sessions"))).scalar_one()
        assert n == 0


async def test_start_match_rebuilt_resets_seq(db_maker):
    agent_id, _ = await _seed_agent(db_maker, seq=7)
    lib_id = await _seed_library(db_maker, agent_id)
    await _mk_item(db_maker, lib_id, "a.mkv")
    digest = manifest_digest([_mrow("a.mkv")])
    res = await _start(db_maker, agent_id, "/srv/media", digest, 1, rebuilt=True)
    assert res["status"] == "match"
    async with db_maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.last_contiguous_seq_no == 0  # §4.2 local-rebuilt -> seq reset
        assert a.last_reconcile_at is not None


async def test_start_mismatch_opens_session(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    lib_id = await _seed_library(db_maker, agent_id)
    await _mk_item(db_maker, lib_id, "a.mkv")
    # agent claims a different corpus (count/digest differ) -> mismatch + session
    res = await _start(db_maker, agent_id, "/srv/media", "deadbeef", 3)
    assert res["status"] == "mismatch" and res["session_id"]
    async with db_maker() as s:
        row = (await s.execute(select(AgentReconcileSession))).scalar_one()
        assert str(row.id) == res["session_id"]
        assert row.library_ref == "/srv/media"
        a = await s.get(Agent, agent_id)
        assert a.last_reconcile_at is None  # NOT stamped on a mismatch


async def test_start_unknown_library_ref_is_mismatch(db_maker):
    # A brand-new/renamed root: no central library yet -> empty projection, a
    # non-empty agent digest mismatches into a session (reconciles into existence).
    agent_id, _ = await _seed_agent(db_maker)
    res = await _start(db_maker, agent_id, "/brand/new", "abc123", 10)
    assert res["status"] == "mismatch" and res["session_id"]


async def test_start_supersedes_prior_session(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _seed_library(db_maker, agent_id)
    r1 = await _start(db_maker, agent_id, "/srv/media", "x", 1)
    r2 = await _start(db_maker, agent_id, "/srv/media", "y", 2)
    assert r1["session_id"] != r2["session_id"]
    async with db_maker() as s:
        # exactly one live session per agent (unique(agent_id))
        rows = (await s.execute(select(AgentReconcileSession))).scalars().all()
        assert len(rows) == 1 and str(rows[0].id) == r2["session_id"]
    # the superseded session id is now unknown -> 404 on rows
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _rows(db_maker, agent_id, r1["session_id"], [_mrow("a")])
    assert exc.value.reason == "unknown_session"


# --------------------------------------------------------------------------- #
# reconcile_stage_rows — staging accumulation + idempotency                    #
# --------------------------------------------------------------------------- #
async def test_rows_accumulate_and_page_is_idempotent(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _seed_library(db_maker, agent_id)
    r = await _start(db_maker, agent_id, "/srv/media", "x", 1)
    sid = r["session_id"]
    assert await _rows(db_maker, agent_id, sid, [_mrow("a"), _mrow("b")]) == 2
    assert await _rows(db_maker, agent_id, sid, [_mrow("c")]) == 3
    # re-send an existing page (updated fields) -> upsert, count unchanged
    assert await _rows(db_maker, agent_id, sid, [_mrow("a", size=999)]) == 3
    async with db_maker() as s:
        staged = (
            await s.execute(
                select(AgentReconcileStaging.rel_path, AgentReconcileStaging.size)
                .where(AgentReconcileStaging.session_id == uuid.UUID(sid))
                .order_by(AgentReconcileStaging.rel_path)
            )
        ).all()
        assert [(x.rel_path, x.size) for x in staged] == [("a", 999), ("b", 100), ("c", 100)]
        sess = (await s.execute(select(AgentReconcileSession))).scalar_one()
        assert sess.staged_rows == 3


async def test_rows_unknown_session_404(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _rows(db_maker, agent_id, str(uuid.uuid4()), [_mrow("a")])
    assert exc.value.reason == "unknown_session"


async def test_rows_wrong_agent_404(db_maker):
    a1, _ = await _seed_agent(db_maker, name="a1")
    a2, _ = await _seed_agent(db_maker, name="a2")
    await _seed_library(db_maker, a1)
    r = await _start(db_maker, a1, "/srv/media", "x", 1)
    # a2 must not touch a1's session
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _rows(db_maker, a2, r["session_id"], [_mrow("a")])
    assert exc.value.reason == "unknown_session"


async def test_expired_session_404_and_swept(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _seed_library(db_maker, agent_id)
    r = await _start(db_maker, agent_id, "/srv/media", "x", 1)
    sid = r["session_id"]
    # Age the session past a tiny TTL by rewinding started_at.
    async with db_maker() as s:
        await s.execute(
            text("UPDATE agent_reconcile_sessions SET started_at = :t"),
            {"t": datetime.now(UTC) - timedelta(hours=2)},
        )
        await s.commit()
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _rows(db_maker, agent_id, sid, [_mrow("a")], ttl=3600)
    assert exc.value.reason == "unknown_session"
    # expired session was deleted on access
    async with db_maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM agent_reconcile_sessions"))).scalar_one()
        assert n == 0


async def test_start_sweeps_expired_sessions(db_maker):
    # A stale session belonging to ANOTHER agent is opportunistically swept at start.
    stale_agent, _ = await _seed_agent(db_maker, name="stale")
    await _seed_library(db_maker, stale_agent, name="stale-lib")
    old = await _start(db_maker, stale_agent, "/srv/media", "x", 1)
    async with db_maker() as s:
        await s.execute(
            text("UPDATE agent_reconcile_sessions SET started_at = :t"),
            {"t": datetime.now(UTC) - timedelta(hours=5)},
        )
        await s.commit()
    fresh_agent, _ = await _seed_agent(db_maker, name="fresh")
    await _start(db_maker, fresh_agent, "/other", "y", 2)
    async with db_maker() as s:
        ids = [str(x) for x in (await s.execute(select(AgentReconcileSession.id))).scalars()]
        assert old["session_id"] not in ids  # the stale one was swept


# --------------------------------------------------------------------------- #
# reconcile_finish — the anti-join, every counter exercised                    #
# --------------------------------------------------------------------------- #
async def test_finish_full_antijoin_all_counters(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    lib_id = await _seed_library(db_maker, agent_id)
    a = agent_id
    # central state (rel_path -> reconcile outcome noted alongside)
    await _mk_item(db_maker, lib_id, "keep.mkv", source_agent_id=a)  # unchanged
    await _mk_item(db_maker, lib_id, "changed.mkv", size=100, source_agent_id=a)  # updated
    await _mk_item(db_maker, lib_id, "gone.mkv", source_agent_id=a)  # tombstoned
    await _mk_item(
        db_maker, lib_id, "back.mkv", status=ItemStatus.missing, source_agent_id=a
    )  # reactivated
    await _mk_item(
        db_maker, lib_id, "trash.mkv", status=ItemStatus.trashed, source_agent_id=a
    )  # trashed_conflict

    manifest = [
        _mrow("keep.mkv"),
        _mrow("changed.mkv", size=200),
        _mrow("back.mkv"),
        _mrow("trash.mkv"),
        _mrow("new.mkv"),
    ]
    digest = manifest_digest(manifest)

    r = await _start(db_maker, agent_id, "/srv/media", digest, len(manifest))
    assert r["status"] == "mismatch"
    sid = r["session_id"]
    await _rows(db_maker, agent_id, sid, manifest)
    res = await _finish(db_maker, agent_id, sid, digest, len(manifest))

    assert res["status"] == "reconciled"
    assert res["upserted"] == 1          # new.mkv
    assert res["updated"] == 1           # changed.mkv
    assert res["tombstoned"] == 1        # gone.mkv
    assert res["reactivated"] == 1       # back.mkv
    assert res["trashed_conflicts"] == 1  # trash.mkv left alone
    assert res["unchanged"] == 1         # keep.mkv

    statuses = await _statuses(db_maker, lib_id)
    assert statuses == {
        "keep.mkv": "active",
        "changed.mkv": "active",
        "gone.mkv": "missing",       # tombstoned
        "back.mkv": "active",        # reactivated
        "trash.mkv": "trashed",      # untouched (user intent wins)
        "new.mkv": "active",         # created
    }
    async with db_maker() as s:
        # updated row took the new size; watermark stamped; session dropped
        row = (
            await s.execute(select(Item).where(Item.rel_path == "changed.mkv"))
        ).scalar_one()
        assert row.size == 200
        a = await s.get(Agent, agent_id)
        assert a.last_reconcile_at is not None
        n = (await s.execute(text("SELECT count(*) FROM agent_reconcile_sessions"))).scalar_one()
        assert n == 0
        stg = (await s.execute(text("SELECT count(*) FROM agent_reconcile_staging"))).scalar_one()
        assert stg == 0  # cascade-cleared with the session


async def test_finish_creates_library_for_new_root(db_maker):
    # No central library exists yet; finish provisions it and upserts the manifest.
    agent_id, _ = await _seed_agent(db_maker)
    manifest = [_mrow("movies/x.mkv"), _mrow("movies/y.mkv")]
    digest = manifest_digest(manifest)
    r = await _start(db_maker, agent_id, "/srv/media", digest, 2)
    await _rows(db_maker, agent_id, r["session_id"], manifest)
    res = await _finish(db_maker, agent_id, r["session_id"], digest, 2)
    assert res["upserted"] == 2
    async with db_maker() as s:
        lib = (await s.execute(select(Library))).scalar_one()
        assert lib.agent_library_ref == "/srv/media"
        assert str(lib.source_agent_id) == str(agent_id)
        items = (await s.execute(select(Item))).scalars().all()
        assert {i.rel_path for i in items} == {"movies/x.mkv", "movies/y.mkv"}
        assert all(i.media_type.value == "video" for i in items)
        assert all(i.path_scope is not None for i in items)


async def test_finish_reset_seq_zeroes_watermark(db_maker):
    agent_id, _ = await _seed_agent(db_maker, seq=9)
    await _seed_library(db_maker, agent_id)
    manifest = [_mrow("a.mkv")]
    digest = manifest_digest(manifest)
    r = await _start(db_maker, agent_id, "/srv/media", digest, 1)
    await _rows(db_maker, agent_id, r["session_id"], manifest)
    await _finish(db_maker, agent_id, r["session_id"], digest, 1, reset_seq=True)
    async with db_maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.last_contiguous_seq_no == 0


async def test_finish_digest_mismatch_deletes_session(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _seed_library(db_maker, agent_id)
    manifest = [_mrow("a.mkv")]
    digest = manifest_digest(manifest)
    r = await _start(db_maker, agent_id, "/srv/media", digest, 1)
    sid = r["session_id"]
    await _rows(db_maker, agent_id, sid, manifest)
    # finish asserts a DIFFERENT digest than what was staged -> 409 + session gone
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _finish(db_maker, agent_id, sid, "wrong-digest", 1)
    assert exc.value.reason == "digest_mismatch"
    async with db_maker() as s:
        n = (await s.execute(text("SELECT count(*) FROM agent_reconcile_sessions"))).scalar_one()
        assert n == 0
        # nothing was applied
        assert (await s.execute(text("SELECT count(*) FROM items"))).scalar_one() == 0


async def test_finish_row_count_mismatch_is_digest_mismatch(db_maker):
    agent_id, _ = await _seed_agent(db_maker)
    await _seed_library(db_maker, agent_id)
    manifest = [_mrow("a.mkv")]
    digest = manifest_digest(manifest)
    r = await _start(db_maker, agent_id, "/srv/media", digest, 1)
    await _rows(db_maker, agent_id, r["session_id"], manifest)
    # correct digest but a lying row_count -> still refused
    with pytest.raises(agentsync.ReconcileError) as exc:
        await _finish(db_maker, agent_id, r["session_id"], digest, 5)
    assert exc.value.reason == "digest_mismatch"


# --------------------------------------------------------------------------- #
# Endpoint — gate / auth / caps / TTL / 409 / defer-after-commit               #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)

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


async def test_endpoint_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings, _ = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": "x", "row_count": 0},
        headers=_auth(fp),
    )
    assert r.status_code == 404


async def test_endpoint_auth(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)
    body = {"library_ref": "/srv/media", "digest": "x", "row_count": 0}
    url = f"/api/v1/agents/{agent_id}/reconcile/start"
    # missing bearer
    assert (await c.post(url, json=body)).status_code == 401
    # wrong bearer
    r = await c.post(url, json=body, headers=_auth("nope"))
    assert r.status_code == 401


async def test_endpoint_match_and_mismatch_flow(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)
    lib_id = await _seed_library(maker, agent_id)
    await _mk_item(maker, lib_id, "a.mkv")
    digest = manifest_digest([_mrow("a.mkv")])
    # match
    r = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": digest, "row_count": 1},
        headers=_auth(fp),
    )
    assert r.status_code == 200 and r.json()["status"] == "match"
    # mismatch -> session id
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": "nope", "row_count": 9},
        headers=_auth(fp),
    )
    assert r2.status_code == 200 and r2.json()["status"] == "mismatch"
    assert r2.json()["session_id"]


async def test_endpoint_page_cap_413(client, monkeypatch):
    c, maker, settings, _ = client
    agent_id, fp = await _seed_agent(maker)
    await _seed_library(maker, agent_id)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": "x", "row_count": 1},
        headers=_auth(fp),
    )
    sid = r.json()["session_id"]
    monkeypatch.setattr(settings, "agent_reconcile_page_max", 2)
    rows = [{"rel_path": f"f{i}", "size": 1, "mtime": T0} for i in range(3)]
    rr = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{sid}/rows",
        json={"rows": rows},
        headers=_auth(fp),
    )
    assert rr.status_code == 413


async def test_endpoint_rows_unknown_session_404(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{uuid.uuid4()}/rows",
        json={"rows": [{"rel_path": "a", "size": 1, "mtime": T0}]},
        headers=_auth(fp),
    )
    assert r.status_code == 404


async def test_endpoint_finish_digest_mismatch_409(client):
    c, maker, _, _ = client
    agent_id, fp = await _seed_agent(maker)
    await _seed_library(maker, agent_id)
    manifest = [_mrow("a.mkv")]
    digest = manifest_digest(manifest)
    start = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": digest, "row_count": 1},
        headers=_auth(fp),
    )
    sid = start.json()["session_id"]
    await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{sid}/rows",
        json={"rows": [{"rel_path": "a.mkv", "size": 100, "mtime": T0, "quick_hash": "q"}]},
        headers=_auth(fp),
    )
    fin = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{sid}/finish",
        json={"digest": "WRONG", "row_count": 1},
        headers=_auth(fp),
    )
    assert fin.status_code == 409
    assert fin.json() == {"reason": "digest_mismatch"}


async def test_endpoint_finish_happy_path_and_defer_after_commit(client):
    c, maker, _, calls = client
    agent_id, fp = await _seed_agent(maker)
    lib_id = await _seed_library(maker, agent_id)
    await _mk_item(maker, lib_id, "old.mkv", source_agent_id=agent_id)  # -> tombstoned
    manifest = [_mrow("a.mkv", quick_hash="q"), _mrow("b.mkv", quick_hash="q")]
    digest = manifest_digest(manifest)
    start = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/start",
        json={"library_ref": "/srv/media", "digest": digest, "row_count": 2},
        headers=_auth(fp),
    )
    sid = start.json()["session_id"]
    rows = [
        {"rel_path": "a.mkv", "size": 100, "mtime": T0, "quick_hash": "q"},
        {"rel_path": "b.mkv", "size": 100, "mtime": T0, "quick_hash": "q"},
    ]
    rr = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{sid}/rows",
        json={"rows": rows},
        headers=_auth(fp),
    )
    assert rr.status_code == 200 and rr.json()["staged"] == 2
    fin = await c.post(
        f"/api/v1/agents/{agent_id}/reconcile/{sid}/finish",
        json={"digest": digest, "row_count": 2},
        headers=_auth(fp),
    )
    assert fin.status_code == 200, fin.text
    body = fin.json()
    assert body == {
        "status": "reconciled",
        "upserted": 2,
        "tombstoned": 1,
        "reactivated": 0,
        "updated": 0,
        "trashed_conflicts": 0,
        "unchanged": 0,
    }
    # index_sync deferred AFTER commit with the touched ids (2 created + 1 tombstoned),
    # each resolvable as a committed row in a fresh session.
    assert len(calls) == 1 and len(calls[0]) == 3
    async with maker() as s:
        for iid in calls[0]:
            assert await s.get(Item, uuid.UUID(iid)) is not None


# --------------------------------------------------------------------------- #
# Purge-safety watermark gate (§4.5) in worker.purge_recycle_bin               #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def purge_env(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "recycle_retention_days", 30)
    deleted_docs: list[list[str]] = []

    async def _fake_delete_docs(ids):
        deleted_docs.append(list(ids))

    import filearr.search as search_mod

    monkeypatch.setattr(search_mod, "delete_docs", _fake_delete_docs)
    return db_maker, deleted_docs


async def _run_purge() -> int:
    return await worker_mod.purge_recycle_bin(0)


async def test_purge_holds_never_reconciled_live_agent(purge_env):
    maker, _ = purge_env
    agent_id, _ = await _seed_agent(maker)  # live, last_reconcile_at NULL
    lib_id = await _seed_library(maker, agent_id)
    old = datetime.now(UTC) - timedelta(days=40)
    await _mk_item(
        maker, lib_id, "trash.mkv", status=ItemStatus.trashed,
        source_agent_id=agent_id, deleted_at=old,
    )
    assert await _run_purge() == 0  # blocked by the watermark
    assert (await _statuses(maker, lib_id)) == {"trash.mkv": "trashed"}


async def test_purge_holds_reconcile_older_than_deletion(purge_env):
    maker, _ = purge_env
    deleted_at = datetime.now(UTC) - timedelta(days=40)
    reconcile_at = datetime.now(UTC) - timedelta(days=45)  # BEFORE the deletion
    agent_id, _ = await _seed_agent(maker, last_reconcile_at=reconcile_at)
    lib_id = await _seed_library(maker, agent_id)
    await _mk_item(
        maker, lib_id, "trash.mkv", status=ItemStatus.trashed,
        source_agent_id=agent_id, deleted_at=deleted_at,
    )
    assert await _run_purge() == 0  # last sweep predates the deletion -> held


async def test_purge_allows_fresh_reconcile(purge_env):
    maker, _ = purge_env
    deleted_at = datetime.now(UTC) - timedelta(days=40)
    reconcile_at = datetime.now(UTC) - timedelta(days=1)  # AFTER the deletion
    agent_id, _ = await _seed_agent(maker, last_reconcile_at=reconcile_at)
    lib_id = await _seed_library(maker, agent_id)
    await _mk_item(
        maker, lib_id, "trash.mkv", status=ItemStatus.trashed,
        source_agent_id=agent_id, deleted_at=deleted_at,
    )
    assert await _run_purge() == 1  # deletion observed -> purged
    assert (await _statuses(maker, lib_id)) == {}


async def test_purge_allows_revoked_agent(purge_env):
    maker, _ = purge_env
    agent_id, _ = await _seed_agent(maker, revoked=True)  # revoked, never reconciled
    lib_id = await _seed_library(maker, agent_id, name="revoked-lib")
    await _mk_item(
        maker, lib_id, "trash.mkv", status=ItemStatus.trashed,
        source_agent_id=agent_id, deleted_at=datetime.now(UTC) - timedelta(days=40),
    )
    assert await _run_purge() == 1  # a revoked agent never blocks purge


async def test_purge_ignores_non_agent_item(purge_env):
    maker, _ = purge_env
    async with maker() as s:
        lib = Library(name="local", root_path="/local")
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    await _mk_item(
        maker, lib_id, "trash.mkv", status=ItemStatus.trashed,
        source_agent_id=None, deleted_at=datetime.now(UTC) - timedelta(days=40),
    )
    assert await _run_purge() == 1  # local item unaffected by the agent gate
