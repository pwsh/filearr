"""T11 -- error surfacing.

Covers:
  * sanitize_error: control-char stripping + length cap (log/UI injection defense)
  * corrupt file end-to-end: _extract_error lands in metadata -> visible in the
    library error count + the /libraries/{id}/errors failing-items endpoint
  * best-effort per-run counter: extract carrying scan_run_id atomically bumps
    ScanRun.stats.extract_errors (and increments race-free across two workers)
  * failed ScanRun retains stats.error (sanitized) via the crash handler, and the
    SSE stream emits an `error` event for a failed scan
  * /system/failed-jobs shape + cap at 100
  * PATCH /libraries null-clears hash_full_max_bytes (model_fields_set fix)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.asyncio


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# sanitize_error (pure unit, no DB)                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_sanitize_strips_control_chars_and_caps():
    from filearr.errors import MAX_ERROR_CHARS, sanitize_error

    # NUL + ANSI escape + CR/LF collapse; visible text survives.
    raw = "bad \x00file\x1b[31mRED\x1b[0m\r\nname"
    out = sanitize_error(raw)
    assert "\x00" not in out and "\x1b" not in out and "\r" not in out and "\n" not in out
    assert "RED" in out and "bad" in out

    long = "x" * 5000
    capped = sanitize_error(long)
    assert len(capped) <= MAX_ERROR_CHARS
    assert capped.endswith("…")


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_sanitize_coerces_non_str():
    from filearr.errors import sanitize_error

    assert sanitize_error(ValueError("boom\x07")) == "boom"
    assert sanitize_error(None) == "None"


# --------------------------------------------------------------------------- #
# DB-wired fixtures (migrated schema on the shared pgserver)                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def wired(pg_uri, monkeypatch):
    from filearr import db as db_mod
    from filearr.api import scans as scans_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM api_keys"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(scans_mod, "SessionLocal", maker)

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    yield {"app": app, "maker": maker}
    app.dependency_overrides.clear()
    await engine.dispose()


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def _mk_library(maker, **kw):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name=kw.pop("name", "lib"), root_path=kw.pop("root_path", "/d"), **kw)
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path, *, metadata=None, status="active"):
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="video", file_group="video",
            status=status,
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp4",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=metadata or {},
        )
        s.add(item)
        await s.commit()
        return item.id


# --------------------------------------------------------------------------- #
# Corrupt file end-to-end: count + failing-items endpoint                      #
# --------------------------------------------------------------------------- #
async def test_error_count_and_failing_items_endpoint(wired):
    maker = wired["maker"]
    lib = await _mk_library(maker, name="movies")
    await _mk_item(maker, lib, "ok.mp4", metadata={"codec": "h264"})
    await _mk_item(
        maker, lib, "bad.mp4",
        metadata={"_extract_error": "ffprobe failed on \x1b[31mbad\x1b[0m file"},
    )
    # a MISSING item with an error must NOT be counted (status filter)
    await _mk_item(
        maker, lib, "gone.mp4", metadata={"_extract_error": "x"}, status="missing"
    )

    async with _client(wired["app"]) as c:
        r = await c.get(f"/api/v1/libraries/{lib}/errors")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert len(body["items"]) == 1
        it = body["items"][0]
        assert it["rel_path"] == "bad.mp4"
        # sanitized: no NUL byte crosses the API boundary
        assert "\x1b" not in it["error"] and "ffprobe failed" in it["error"]

        # /stats exposes the same count keyed by library id
        st = (await c.get("/api/v1/stats")).json()
        assert st["extract_errors"][str(lib)] == 1

    # unknown library -> 404
    async with _client(wired["app"]) as c:
        r = await c.get("/api/v1/libraries/00000000-0000-0000-0000-000000000000/errors")
        assert r.status_code == 404


async def test_failing_items_pagination_cap(wired):
    maker = wired["maker"]
    lib = await _mk_library(maker)
    for i in range(5):
        await _mk_item(maker, lib, f"f{i}.mp4", metadata={"_extract_error": f"e{i}"})
    async with _client(wired["app"]) as c:
        r = await c.get(f"/api/v1/libraries/{lib}/errors?limit=2&offset=0")
        body = r.json()
        assert body["count"] == 5
        assert len(body["items"]) == 2
        # cap: limit>100 is clamped (still returns, never explodes)
        r2 = await c.get(f"/api/v1/libraries/{lib}/errors?limit=99999")
        assert r2.status_code == 200


# --------------------------------------------------------------------------- #
# Best-effort per-run atomic counter                                          #
# --------------------------------------------------------------------------- #
async def test_per_run_counter_increments_atomically(wired, monkeypatch):
    from filearr.models import ScanRun
    from filearr.tasks import extract as extract_mod
    from filearr.tasks import index_sync

    maker = wired["maker"]
    monkeypatch.setattr(extract_mod, "SessionLocal", maker)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop)

    lib = await _mk_library(maker)
    async with maker() as s:
        # UI-T14: a staged pipeline defers extraction until the scan has finished
        # walking, so by the time these extract jobs run their ScanRun is terminal.
        # Mark it 'finished' -> the reschedule gate does not fire and attribution
        # (the jsonb_set counter, matched by run id) still works.
        run = ScanRun(library_id=lib, status="finished", stats={})
        s.add(run)
        await s.commit()
        run_id = run.id

    # Two corrupt items (media type with no real file -> extractor raises OSError
    # / parser error), extracted concurrently, both carrying the same scan_run_id.
    id1 = await _mk_item(maker, lib, "a.mp4")
    id2 = await _mk_item(maker, lib, "b.mp4")

    # Force the extractor to fail deterministically.
    def boom(_path):
        raise RuntimeError("nope")

    monkeypatch.setitem(extract_mod.EXTRACTOR_BY_KIND, "video", boom)

    await asyncio.gather(
        extract_mod.extract_item(str(id1), str(run_id)),
        extract_mod.extract_item(str(id2), str(run_id)),
    )

    async with maker() as s:
        run = (
            await s.execute(text("SELECT stats FROM scan_runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert run["extract_errors"] == 2  # no lost update


async def test_extract_error_is_sanitized_in_metadata(wired, monkeypatch):
    from filearr.tasks import extract as extract_mod
    from filearr.tasks import index_sync

    maker = wired["maker"]
    monkeypatch.setattr(extract_mod, "SessionLocal", maker)

    async def _noop(**_kw):
        return None

    monkeypatch.setattr(index_sync.sync_items, "defer_async", _noop)

    lib = await _mk_library(maker)
    iid = await _mk_item(maker, lib, "c.mp4")

    def boom(_path):
        raise RuntimeError("bad\x00\x1b[31mstuff")

    monkeypatch.setitem(extract_mod.EXTRACTOR_BY_KIND, "video", boom)
    await extract_mod.extract_item(str(iid))  # no scan_run_id -> still records error

    async with maker() as s:
        meta = (
            await s.execute(text("SELECT metadata FROM items WHERE id = :i"), {"i": iid})
        ).scalar_one()
    err = meta["_extract_error"]
    assert "\x00" not in err and "\x1b" not in err and "stuff" in err


# --------------------------------------------------------------------------- #
# Failed ScanRun retains stats.error + SSE error event                        #
# --------------------------------------------------------------------------- #
async def test_scan_crash_retains_sanitized_error(wired, monkeypatch):
    from filearr.tasks import scan as scan_mod

    maker = wired["maker"]
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    # Signature must track scan_library's call into _scan_body. W9 added the
    # `recursive` kwarg and this stub was not updated, so the call raised
    # TypeError before reaching the assertions below — silently leaving
    # architecture invariant 7 (a crashed scan MUST end `failed`, never
    # `running`) untested. **kwargs keeps it from rotting again.
    async def _boom_body(session, library, run, scope_rel=None, **kwargs):
        raise RuntimeError("scan exploded\x00 with control\x07 chars")

    monkeypatch.setattr(scan_mod, "_scan_body", _boom_body)

    lib = await _mk_library(maker, name="crashy")
    with pytest.raises(RuntimeError):
        await scan_mod.scan_library(str(lib))

    async with maker() as s:
        run = (
            await s.execute(
                text("SELECT status, stats FROM scan_runs ORDER BY started_at DESC LIMIT 1")
            )
        ).one()
    assert run.status == "failed"
    assert "scan exploded" in run.stats["error"]
    assert "\x00" not in run.stats["error"] and "\x07" not in run.stats["error"]


async def test_sse_emits_error_event_for_failed_scan(wired):
    from filearr.models import Library, ScanRun

    maker = wired["maker"]
    async with maker() as s:
        lib = Library(name="l", root_path="/d")
        s.add(lib)
        await s.flush()
        run = ScanRun(
            library_id=lib.id, status="failed",
            stats={"error": "boom happened", "seen": 3},
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
        )
        s.add(run)
        await s.commit()
        rid = run.id

    async with _client(wired["app"]) as c:
        events = []
        async with c.stream("GET", f"/api/v1/scans/{rid}/events") as resp:
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "event: done" in buf:
                    break
            for block in buf.split("\n\n"):
                if block.startswith("event:"):
                    ev = block.split("\n")[0].split("event:")[1].strip()
                    events.append(ev)
    assert "error" in events
    assert "done" in events


# --------------------------------------------------------------------------- #
# /system/failed-jobs shape + cap                                             #
# --------------------------------------------------------------------------- #
async def test_failed_jobs_endpoint_shape_and_cap(wired):
    # procrastinate schema may not be present in the migrated app DB -> endpoint
    # returns an empty page (total, never raises). FIX-8: the shape is now a
    # paginated {items, total, limit, offset} envelope and limit is capped at 100.
    async with _client(wired["app"]) as c:
        r = await c.get("/api/v1/system/failed-jobs?limit=99999")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["items"], list)
        assert body["total"] == 0
        assert body["limit"] == 100  # capped
        assert body["offset"] == 0


async def test_failed_jobs_query_reads_procrastinate(pg_uri):
    """Directly exercise errors.failed_jobs against a DB carrying the real
    procrastinate schema + a failed job row."""
    import psycopg
    from procrastinate import PsycopgConnector

    from filearr.errors import failed_jobs
    from filearr.worker import proc_app

    connector = PsycopgConnector(conninfo=pg_uri)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
    proc_app.connector = original

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
        conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
            "VALUES ('extract', 'filearr.tasks.extract.extract_item', '{}'::jsonb, "
            "'failed'::procrastinate_job_status)"
        )
        conn.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
            "VALUES ('extract', 'filearr.tasks.extract.extract_item', '{}'::jsonb, "
            "'succeeded'::procrastinate_job_status)"
        )

    engine = create_async_engine(_psycopg3(pg_uri))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        jobs = await failed_jobs(s, limit=100)
    await engine.dispose()

    assert len(jobs) == 1
    j = jobs[0]
    assert j["queue"] == "extract"
    assert j["task"] == "filearr.tasks.extract.extract_item"
    assert j["status"] == "failed"
    assert j["error"] is None  # procrastinate 3.9 stores no per-job error text


# --------------------------------------------------------------------------- #
# PATCH null-clear fix                                                         #
# --------------------------------------------------------------------------- #
async def test_patch_null_clears_hash_full_max_bytes(wired):
    lib = await _mk_library(wired["maker"], name="hashy")
    async with _client(wired["app"]) as c:
        # set it
        r = await c.patch(f"/api/v1/libraries/{lib}", json={"hash_full_max_bytes": 123456})
        assert r.json()["hash_full_max_bytes"] == 123456
        # explicit null clears it (was silently dropped before the fix)
        r = await c.patch(f"/api/v1/libraries/{lib}", json={"hash_full_max_bytes": None})
        assert r.status_code == 200
        assert r.json()["hash_full_max_bytes"] is None
        # absent field leaves other fields untouched (name unchanged, ceiling stays null)
        r = await c.patch(f"/api/v1/libraries/{lib}", json={"name": "hashy2"})
        body = r.json()
        assert body["name"] == "hashy2"
        assert body["hash_full_max_bytes"] is None
