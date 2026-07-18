"""SSE scan-progress endpoint (`GET /scans/{id}/events`).

Exercises the native `EventSourceResponse` path over an in-process ASGI
transport (httpx streaming): progress events arrive, the stream closes on a
terminal status, keepalive pings are emitted when idle, and Bearer-scope auth
is enforced when enabled (header + `?api_key=` query param, since EventSource
can't set headers).
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api import scans as scans_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import ApiKey, Library, ScanRun

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired(pg_uri, monkeypatch):
    """A test engine + app whose sessions all point at the migrated pg_uri DB.

    `filearr.db.SessionLocal` is bound at import time; the SSE generator uses it
    directly, so we repoint it (and the copy the scans module imported) at a
    sessionmaker over the test engine, and override the request-scoped
    `get_session` dependency too.
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(scans_mod, "SessionLocal", maker)

    get_settings.cache_clear()
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session

    yield {"app": app, "maker": maker}

    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_scan(maker, status="running", stats=None):
    async with maker() as s:
        lib = Library(name="lib", root_path="/data")
        s.add(lib)
        await s.flush()
        run = ScanRun(
            library_id=lib.id,
            status=status,
            stats=stats or {"seen": 0, "new": 0, "changed": 0},
            started_at=datetime.now(UTC),
        )
        s.add(run)
        await s.commit()
        return run.id, lib.id


async def _mk_key(maker, scopes=("read",)):
    full = "ck_testkey_" + "x" * 20
    async with maker() as s:
        s.add(
            ApiKey(
                name="t",
                prefix="ck_test",
                key_hash=hashlib.sha256(full.encode()).hexdigest(),
                scopes=list(scopes),
            )
        )
        await s.commit()
    return full


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _read_events(resp, *, want_done=True, timeout=15.0):
    """Parse an SSE byte stream into a list of (event, data) tuples until a
    `done`/`error` event (or timeout)."""
    events: list[tuple[str, str]] = []
    pings = 0
    buf = ""

    async def _pump():
        nonlocal buf, pings
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                lines = raw.split("\n")
                if all(ln.startswith(":") for ln in lines if ln):
                    pings += 1
                    continue
                ev = "message"
                data_parts = []
                for ln in lines:
                    if ln.startswith("event:"):
                        ev = ln[len("event:"):].strip()
                    elif ln.startswith("data:"):
                        data_parts.append(ln[len("data:"):].strip())
                events.append((ev, "\n".join(data_parts)))
                if ev in ("done", "error") and want_done:
                    return

    await asyncio.wait_for(_pump(), timeout=timeout)
    return events, pings


@pytest.mark.asyncio
async def test_progress_then_done(wired, monkeypatch):
    """Progress ticks as stats change, then a terminal `done` closes the stream."""
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(scans_mod, "_POLL_INTERVAL", 0.05)
    maker = wired["maker"]
    scan_id, _ = await _mk_scan(maker, status="running", stats={"seen": 10, "new": 10})

    async def _advance():
        # let the first progress event be emitted, then flip to a terminal state
        await asyncio.sleep(0.25)
        async with maker() as s:
            run = await s.get(ScanRun, scan_id)
            run.status = "finished"
            run.stats = {"seen": 42, "new": 42, "changed": 0, "missing": 0}
            run.finished_at = datetime.now(UTC)
            await s.commit()

    async with _client(wired["app"]) as client:
        task = asyncio.create_task(_advance())
        async with client.stream("GET", f"/api/v1/scans/{scan_id}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events, _ = await _read_events(resp)
        await task

    kinds = [e for e, _ in events]
    assert "progress" in kinds
    assert kinds[-1] == "done"
    prog = json.loads(next(d for e, d in events if e == "progress"))
    assert prog["status"] == "running"
    assert prog["seen"] == 10
    assert "rate" in prog and "elapsed" in prog
    done = json.loads(events[-1][1])
    assert done["status"] == "finished"
    assert done["seen"] == 42


@pytest.mark.asyncio
async def test_terminal_immediately_closes(wired, monkeypatch):
    """A scan that is already terminal emits exactly one `done` and closes."""
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(scans_mod, "_POLL_INTERVAL", 0.05)
    maker = wired["maker"]
    scan_id, _ = await _mk_scan(maker, status="cancelled", stats={"seen": 3, "aborted": True})

    async with _client(wired["app"]) as client:
        async with client.stream("GET", f"/api/v1/scans/{scan_id}/events") as resp:
            events, _ = await _read_events(resp)
    assert len(events) == 1
    assert events[0][0] == "done"
    assert json.loads(events[0][1])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unknown_scan_errors(wired, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(scans_mod, "_POLL_INTERVAL", 0.05)
    import uuid

    async with _client(wired["app"]) as client:
        async with client.stream("GET", f"/api/v1/scans/{uuid.uuid4()}/events") as resp:
            events, _ = await _read_events(resp)
    assert events[-1][0] == "error"


@pytest.mark.asyncio
async def test_heartbeat_emitted_when_idle(wired, monkeypatch):
    """When the scan is idle (no stat changes), the framework inserts keepalive
    `: ping` comments so proxies don't drop the stream."""
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(scans_mod, "_POLL_INTERVAL", 0.05)
    # Shrink the framework ping interval so the test is fast.
    import fastapi.routing as routing_mod
    import fastapi.sse as sse_mod

    monkeypatch.setattr(sse_mod, "_PING_INTERVAL", 0.2, raising=False)
    monkeypatch.setattr(routing_mod, "_PING_INTERVAL", 0.2, raising=False)

    maker = wired["maker"]
    scan_id, _ = await _mk_scan(maker, status="running", stats={"seen": 1})

    async def _finish():
        await asyncio.sleep(0.9)  # stay idle long enough for >=1 ping
        async with maker() as s:
            run = await s.get(ScanRun, scan_id)
            run.status = "finished"
            run.finished_at = datetime.now(UTC)
            await s.commit()

    async with _client(wired["app"]) as client:
        task = asyncio.create_task(_finish())
        async with client.stream("GET", f"/api/v1/scans/{scan_id}/events") as resp:
            _, pings = await _read_events(resp, timeout=15.0)
        await task
    assert pings >= 1


@pytest.mark.asyncio
async def test_auth_enforced_when_enabled(wired, monkeypatch):
    """With auth on: no creds -> 401; valid `?api_key=` -> 200 stream; header
    Bearer also works. Query-param path exists because EventSource can't set
    headers."""
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    monkeypatch.setattr(scans_mod, "_POLL_INTERVAL", 0.05)
    maker = wired["maker"]
    scan_id, _ = await _mk_scan(maker, status="finished", stats={"seen": 1})
    key = await _mk_key(maker, scopes=("read",))

    async with _client(wired["app"]) as client:
        # 1) no creds -> 401
        r = await client.get(f"/api/v1/scans/{scan_id}/events")
        assert r.status_code == 401

        # 2) query-param api_key -> ok
        async with client.stream(
            "GET", f"/api/v1/scans/{scan_id}/events", params={"api_key": key}
        ) as resp:
            assert resp.status_code == 200
            events, _ = await _read_events(resp)
        assert events[-1][0] == "done"

        # 3) header Bearer -> ok
        async with client.stream(
            "GET",
            f"/api/v1/scans/{scan_id}/events",
            headers={"Authorization": f"Bearer {key}"},
        ) as resp:
            assert resp.status_code == 200

        # 4) wrong key -> 401
        r = await client.get(
            f"/api/v1/scans/{scan_id}/events", params={"api_key": "ck_bogus_nope"}
        )
        assert r.status_code == 401
