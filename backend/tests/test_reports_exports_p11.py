"""P11 remainder — xlsx export, background export jobs, scheduled delivery, and
the T10 download-action gate.

* **pure/unit** — xlsx formula-guard + validity, ``require_capability`` matrix,
  scope-snapshot round-trip;
* **integration** (pgserver + alembic) — export job lifecycle (queued→complete→
  download) incl. RBAC re-check + diskguard-critical failure + retention purge +
  reconcile, once-per-occurrence schedule firing, delivery email-attachment vs
  link-fallback + webhook summary, migration round-trip.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import exports as exports_mod
from filearr import report_delivery
from filearr.alerts import dispatch as alerts_dispatch
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    AlertChannel,
    Item,
    Library,
    ReportExport,
    ReportSchedule,
)


@pytest.fixture(autouse=True)
def _no_disk_guard(monkeypatch):
    """FIX-11 guard reads the REAL statvfs (sandbox tmp sits below the 5GB
    floor); force 'ok' by default — the diskguard-critical test overrides
    filearr.exports.diskguard.guard_write itself, which wins over this."""
    from filearr import diskguard

    monkeypatch.setattr(diskguard, "is_critical", lambda *a, **k: False)
    monkeypatch.setattr(diskguard, "guard_write", lambda *a, **k: None)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Pure: xlsx render + formula guard                                           #
# --------------------------------------------------------------------------- #
async def _agen(rows):
    for r in rows:
        yield r


async def test_render_xlsx_is_valid_zip_and_formula_guarded(tmp_path):
    from filearr.reports import render_xlsx_to_path

    path = str(tmp_path / "out.xlsx")
    rows = [
        {"rel_path": "=SUM(A1)", "size": 10},
        {"rel_path": "+cmd|calc", "size": 20},
        {"rel_path": "normal.mp4", "size": 30},
    ]
    n = await render_xlsx_to_path(["rel_path", "size"], _agen(rows), path)
    assert n == 3
    # Valid xlsx = a zip carrying the workbook part.
    assert zipfile.is_zipfile(path)
    with zipfile.ZipFile(path) as zf:
        assert "xl/workbook.xml" in zf.namelist()

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    # Header + 3 rows.
    assert ws["A1"].value == "rel_path"
    # The formula-shaped value is a LITERAL string, never a formula/number.
    c = ws["A2"]
    assert c.value == "=SUM(A1)"
    assert c.data_type == "s"  # string, not formula ('f')
    assert ws["A3"].value == "+cmd|calc"
    assert ws["B2"].value == "10"  # written as string (strings_to_numbers off)


# --------------------------------------------------------------------------- #
# Pure: T10 download-capability matrix                                        #
# --------------------------------------------------------------------------- #
def _ctx(**kw):
    from filearr.security import PermissionContext

    return PermissionContext(**kw)


def test_require_capability_matrix():
    from fastapi import HTTPException

    from filearr import rbac
    from filearr.security import PermissionContext

    # auth-off / admin / API key → unrestricted → no-op.
    PermissionContext(unrestricted=True, action="download").require_capability("download")

    # viewer role: download is outside the ceiling → 403.
    viewer = PermissionContext(
        unrestricted=False, action="download", role=rbac.Role.VIEWER, grants=[]
    )
    with pytest.raises(HTTPException) as ei:
        viewer.require_capability("download")
    assert ei.value.status_code == 403

    # user role, no download grant → 403 (capability absent).
    user_nogrant = PermissionContext(
        unrestricted=False, action="download", role=rbac.Role.USER, grants=[]
    )
    with pytest.raises(HTTPException):
        user_nogrant.require_capability("download")

    # user role WITH a download grant → allowed.
    g = rbac.PathGrant(path="lib_1.movies", action="download", allow=True)
    user_ok = PermissionContext(
        unrestricted=False, action="download", role=rbac.Role.USER, grants=[g]
    )
    user_ok.require_capability("download")  # no raise


def test_scope_snapshot_roundtrip():
    from filearr import rbac
    from filearr.exports import scope_clause_from_snapshot
    from filearr.security import PermissionContext

    g = rbac.PathGrant(path="lib_1.movies", action="download", allow=True)
    ctx = PermissionContext(
        unrestricted=False, action="download", role=rbac.Role.USER, grants=[g]
    )
    snap = ctx.scope_snapshot()
    assert snap["role"] == "user"
    assert snap["grants"][0]["action"] == "download"
    # Rebuild → a non-None SQL clause (a scoped principal is filtered).
    clause = scope_clause_from_snapshot(snap, action="download")
    assert clause is not None
    # Unrestricted snapshot → None.
    assert PermissionContext(unrestricted=True, action="download").scope_snapshot() is None
    assert scope_clause_from_snapshot(None, action="download") is None


# --------------------------------------------------------------------------- #
# Integration fixture                                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def env(pg_uri, monkeypatch, tmp_path):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for tbl in (
            "report_exports",
            "report_schedules",
            "alert_channels",
            "items",
            "libraries",
        ):
            await conn.execute(text(f"DELETE FROM {tbl}"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    # tasks/reports.py binds SessionLocal by value at import time (module-level
    # from-import, codebase convention); patch its copy too so a prior test's
    # torn-down pg (import-order dependent) can't leak in.
    from filearr.tasks import reports as _trep_mod

    monkeypatch.setattr(_trep_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(maker, n=3):
    async with maker() as s:
        lib = Library(name="L", root_path="/data/l")
        s.add(lib)
        await s.commit()
        for i in range(n):
            s.add(
                Item(
                    library_id=lib.id,
                    file_category="video", file_group="video",
                    status="active",
                    path=f"/data/l/f{i}.mp4",
                    rel_path=f"f{i}.mp4",
                    filename=f"f{i}.mp4",
                    extension="mp4",
                    size=100 * (i + 1),
                    mtime=datetime.now(UTC),
                    metadata_={},
                    user_metadata={},
                    external_ids={},
                    tags=[],
                )
            )
        await s.commit()
        return lib.id


# --------------------------------------------------------------------------- #
# Migration round-trip                                                        #
# --------------------------------------------------------------------------- #
async def test_migration_creates_tables(env):
    _client, maker, _settings = env
    async with maker() as s:
        r = await s.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('report_exports','report_schedules')"
            )
        )
        assert {row[0] for row in r} == {"report_exports", "report_schedules"}


# --------------------------------------------------------------------------- #
# Sync xlsx download over HTTP                                                #
# --------------------------------------------------------------------------- #
async def test_sync_xlsx_download(env):
    client, maker, _ = env
    await _seed(maker)
    r = await client.get("/api/v1/reports/largest_files?format=xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert zipfile.is_zipfile(io.BytesIO(r.content))


async def test_sync_xlsx_formula_guard(env):
    client, maker, _ = env
    async with maker() as s:
        lib = Library(name="L", root_path="/data/l")
        s.add(lib)
        await s.commit()
        s.add(
            Item(
                library_id=lib.id, file_category="video", file_group="video", status="active",
                path="/data/l/=danger.mp4", rel_path="=SUM(A1)", filename="=SUM(A1)",
                extension="mp4", size=10, mtime=datetime.now(UTC), metadata_={},
                user_metadata={}, external_ids={}, tags=[],
            )
        )
        await s.commit()
    r = await client.get("/api/v1/reports/largest_files?format=xlsx")
    assert r.status_code == 200
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    ws = wb.active
    # find the rel_path column and assert the =SUM literal string survives
    header = [c.value for c in ws[1]]
    col = header.index("rel_path") + 1
    val = ws.cell(row=2, column=col)
    assert val.value == "=SUM(A1)"
    assert val.data_type == "s"


# --------------------------------------------------------------------------- #
# Background export lifecycle + download + RBAC (auth-off = unrestricted)     #
# --------------------------------------------------------------------------- #
async def test_export_job_lifecycle_and_download(env):
    client, maker, settings = env
    await _seed(maker, 4)
    # enqueue via HTTP (defer to procrastinate is patched out to avoid a worker)
    import filearr.tasks.reports as trep

    async def _noop(_id):
        return 1

    # patch the deferral used inside enqueue_export
    orig = trep.defer_export_job
    trep.defer_export_job = _noop
    try:
        r = await client.post("/api/v1/reports/largest_files/export?format=csv")
        assert r.status_code == 202, r.text
        export_id = r.json()["id"]
        assert r.json()["status"] == "queued"
    finally:
        trep.defer_export_job = orig

    # run the job body directly (simulating the worker)
    async with maker() as s:
        import uuid as _uuid

        res = await exports_mod.run_export(s, _uuid.UUID(export_id), settings)
    assert res["status"] == "complete"

    # status endpoint
    r = await client.get(f"/api/v1/exports/{export_id}")
    body = r.json()
    assert body["status"] == "complete"
    assert body["row_count"] == 4
    assert body["downloadable"] is True

    # download the artifact
    r = await client.get(f"/api/v1/exports/{export_id}/download")
    assert r.status_code == 200
    assert r.text.startswith("rel_path,library")

    # list shows it
    r = await client.get("/api/v1/exports")
    assert any(e["id"] == export_id for e in r.json()["exports"])


async def test_export_job_diskguard_critical_fails(env, monkeypatch):
    _client, maker, settings = env
    await _seed(maker, 2)
    async with maker() as s:
        ex = ReportExport(
            canned_report_key="largest_files", format="csv",
            params={"limit": 10}, status="queued",
        )
        s.add(ex)
        await s.commit()
        ex_id = ex.id

    from filearr import diskguard

    def _boom(path, settings, **kw):
        raise diskguard.DiskGuardError(path, {"free": 0, "pct_free": 0.0, "reason": "test"})

    monkeypatch.setattr("filearr.exports.diskguard.guard_write", _boom)
    async with maker() as s:
        res = await exports_mod.run_export(s, ex_id, settings)
    assert res["status"] == "failed"
    async with maker() as s:
        ex = await s.get(ReportExport, ex_id)
        assert ex.status == "failed"
        assert "disk_full_guard" in (ex.error or "")


async def test_retention_purge_and_reconcile(env, tmp_path):
    _client, maker, settings = env
    # a complete-but-expired export with an artifact file
    art = tmp_path / "art.csv"
    art.write_text("x")
    async with maker() as s:
        ex = ReportExport(
            canned_report_key="largest_files", format="csv", params={},
            status="complete", artifact_path=str(art),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        s.add(ex)
        # a stale 'running' export (crash) started long ago
        stale = ReportExport(
            canned_report_key="largest_files", format="csv", params={},
            status="running", started_at=datetime.now(UTC) - timedelta(hours=3),
        )
        s.add(stale)
        await s.commit()
        ex_id, stale_id = ex.id, stale.id

    async with maker() as s:
        purged = await exports_mod.purge_expired_exports(s, settings)
    assert purged == 1
    assert not art.exists()
    async with maker() as s:
        ex = await s.get(ReportExport, ex_id)
        assert ex.purged_at is not None
        assert ex.artifact_path is None  # row kept, artifact cleared

    async with maker() as s:
        n = await exports_mod.reconcile_stale_exports(s, settings)
    assert n == 1
    async with maker() as s:
        stale = await s.get(ReportExport, stale_id)
        assert stale.status == "failed"


# --------------------------------------------------------------------------- #
# Schedule once-per-occurrence                                                #
# --------------------------------------------------------------------------- #
async def test_schedule_once_per_occurrence(env, monkeypatch):
    _client, maker, _ = env
    import filearr.tasks.reports as trep

    deferred: list[str] = []

    async def _capture(export_id):
        deferred.append(str(export_id))
        return 1

    monkeypatch.setattr(trep, "defer_export_job", _capture)

    async with maker() as s:
        sched = ReportSchedule(
            name="sched", canned_report_key="largest_files", format="csv",
            params={}, cron="* * * * *", enabled=True,
        )
        s.add(sched)
        await s.commit()
        sched_id = sched.id

    tick = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    # two ticks in the SAME minute → fire exactly once
    n1 = await trep.evaluate_report_schedules(tick)
    n2 = await trep.evaluate_report_schedules(tick)
    assert len(n1) == 1
    assert len(n2) == 0
    assert len(deferred) == 1
    # advancing a minute → fires again
    n3 = await trep.evaluate_report_schedules(tick + timedelta(minutes=1))
    assert len(n3) == 1
    assert len(deferred) == 2

    async with maker() as s:
        sched = await s.get(ReportSchedule, sched_id)
        assert sched.last_cron_fired_at is not None
    # exactly two exports created
    async with maker() as s:
        rows = (await s.execute(text("SELECT count(*) FROM report_exports"))).scalar()
        assert rows == 2


# --------------------------------------------------------------------------- #
# Scheduled delivery: email attach vs link, webhook summary                   #
# --------------------------------------------------------------------------- #
async def _make_export_with_file(maker, tmp_path, channel_id, size_bytes):
    art = tmp_path / "deliver.csv"
    art.write_bytes(b"x" * size_bytes)
    async with maker() as s:
        sched = ReportSchedule(
            name="deliverme", canned_report_key="largest_files", format="csv",
            params={}, cron="0 6 * * *", enabled=True, channel_id=channel_id,
        )
        s.add(sched)
        await s.commit()
        ex = ReportExport(
            canned_report_key="largest_files", format="csv", params={},
            status="complete", schedule_id=sched.id, artifact_path=str(art),
            row_count=5, file_size_bytes=size_bytes, delivery_status="pending",
        )
        s.add(ex)
        await s.commit()
        return ex.id


async def test_delivery_email_attaches_when_small(env, monkeypatch, tmp_path):
    _client, maker, settings = env
    async with maker() as s:
        ch = AlertChannel(
            name="mail", type_="email",
            config={"host": "smtp", "from_addr": "a@b.c", "to": "d@e.f"},
            dispatch_locality="central", enabled=True,
        )
        s.add(ch)
        await s.commit()
        ch_id = ch.id

    captured = {}

    async def _fake_email(config, rendered, *, timeout_s=30.0, attachment=None):
        captured["attachment"] = attachment
        captured["body"] = rendered.body_text
        from filearr.alerts.dispatch import DeliveryResult

        return DeliveryResult(ok=True)

    monkeypatch.setattr(report_delivery, "send_email", _fake_email)
    monkeypatch.setattr(settings, "report_email_max_bytes", 10_000)

    ex_id = await _make_export_with_file(maker, tmp_path, ch_id, 100)
    async with maker() as s:
        ex = await s.get(ReportExport, ex_id)
        status = await report_delivery.deliver_scheduled_export(s, ex, settings)
    assert status == "delivered"
    assert captured["attachment"] is not None
    assert captured["attachment"][0].endswith(".csv")


async def test_delivery_email_link_fallback_when_large(env, monkeypatch, tmp_path):
    _client, maker, settings = env
    async with maker() as s:
        ch = AlertChannel(
            name="mail2", type_="email",
            config={"host": "smtp", "from_addr": "a@b.c", "to": "d@e.f"},
            dispatch_locality="central", enabled=True,
        )
        s.add(ch)
        await s.commit()
        ch_id = ch.id

    captured = {}

    async def _fake_email(config, rendered, *, timeout_s=30.0, attachment=None):
        captured["attachment"] = attachment
        captured["body"] = rendered.body_text
        from filearr.alerts.dispatch import DeliveryResult

        return DeliveryResult(ok=True)

    monkeypatch.setattr(report_delivery, "send_email", _fake_email)
    monkeypatch.setattr(settings, "report_email_max_bytes", 10)  # tiny cap

    ex_id = await _make_export_with_file(maker, tmp_path, ch_id, 5000)
    async with maker() as s:
        ex = await s.get(ReportExport, ex_id)
        status = await report_delivery.deliver_scheduled_export(s, ex, settings)
    assert status == "delivered"
    assert captured["attachment"] is None  # over cap → no attachment
    assert "Download:" in captured["body"]  # link fallback


async def test_delivery_webhook_sends_summary_not_file(env, monkeypatch, tmp_path):
    _client, maker, settings = env
    async with maker() as s:
        ch = AlertChannel(
            name="hook", type_="webhook",
            config={"url": "https://example.com/hook"},
            dispatch_locality="central", enabled=True,
        )
        s.add(ch)
        await s.commit()
        ch_id = ch.id

    captured = {}

    async def _fake_webhook(url, payload, **kw):
        captured["url"] = url
        captured["payload"] = payload
        from filearr.alerts.dispatch import DeliveryResult

        return DeliveryResult(ok=True)

    monkeypatch.setattr(alerts_dispatch, "send_webhook", _fake_webhook)

    ex_id = await _make_export_with_file(maker, tmp_path, ch_id, 999)
    async with maker() as s:
        ex = await s.get(ReportExport, ex_id)
        status = await report_delivery.deliver_scheduled_export(s, ex, settings)
    assert status == "delivered"
    assert "download_url" in captured["payload"]
    assert captured["payload"]["row_count"] == 5
    # never inline the file bytes
    assert "content" not in captured["payload"]
    assert "/exports/" in captured["payload"]["download_url"]


# --------------------------------------------------------------------------- #
# P11-T11: per-principal concurrent-export cap (429)                           #
# --------------------------------------------------------------------------- #
async def test_per_principal_export_cap(env, monkeypatch):
    """A scoped principal may hold at most ``export_max_active`` in-flight
    (queued/running) manual exports; the next enqueue is refused with 429. An
    export leaving the active set frees a slot again."""
    import types
    import uuid as _uuid

    from fastapi import HTTPException

    from filearr import rbac
    from filearr.api.exports import enqueue_export
    from filearr.security import PermissionContext

    _client, maker, settings = env
    await _seed(maker, 2)
    monkeypatch.setattr(settings, "export_max_active", 2)

    import filearr.tasks.reports as trep

    async def _noop(_id):
        return 1

    monkeypatch.setattr(trep, "defer_export_job", _noop)

    pid = _uuid.uuid4()
    ctx = PermissionContext(
        unrestricted=False,
        action="download",
        role=rbac.Role.USER,
        grants=[rbac.PathGrant(path="l", action="download", allow=True)],
        principal=types.SimpleNamespace(id=pid),
    )

    async with maker() as s:
        e1 = await enqueue_export(s, ctx, canned_report_key="largest_files", fmt="csv")
        await enqueue_export(s, ctx, canned_report_key="largest_files", fmt="csv")
        assert e1.owner_principal == str(pid)
        # cap of 2 reached → third manual enqueue rejected with 429.
        with pytest.raises(HTTPException) as ei:
            await enqueue_export(s, ctx, canned_report_key="largest_files", fmt="csv")
        assert ei.value.status_code == 429
        # a finished export frees a slot; a new enqueue is allowed again.
        e1.status = "complete"
        await s.commit()
        e3 = await enqueue_export(s, ctx, canned_report_key="largest_files", fmt="csv")
        assert e3.status == "queued"


async def test_unrestricted_actor_not_capped(env, monkeypatch):
    """An unrestricted actor (admin / API key / auth-off) bypasses the cap: the
    env client runs auth-off, so many enqueues in a row all succeed."""
    client, maker, settings = env
    await _seed(maker, 2)
    monkeypatch.setattr(settings, "export_max_active", 1)

    import filearr.tasks.reports as trep

    async def _noop(_id):
        return 1

    monkeypatch.setattr(trep, "defer_export_job", _noop)

    for _ in range(3):
        r = await client.post("/api/v1/reports/largest_files/export?format=csv")
        assert r.status_code == 202, r.text


# --------------------------------------------------------------------------- #
# P11-T10: export download is audited unconditionally                          #
# --------------------------------------------------------------------------- #
async def test_export_download_is_audited(env, monkeypatch):
    """A served artifact download writes a ``report_export_downloaded`` security
    event unconditionally (data-exfiltration carve-out, mirrors transfers)."""
    import uuid as _uuid

    from sqlalchemy import select as _select

    from filearr.models import SecurityEvent

    client, maker, settings = env
    await _seed(maker, 3)

    import filearr.tasks.reports as trep

    async def _noop(_id):
        return 1

    monkeypatch.setattr(trep, "defer_export_job", _noop)

    r = await client.post("/api/v1/reports/largest_files/export?format=csv")
    export_id = r.json()["id"]
    async with maker() as s:
        await exports_mod.run_export(s, _uuid.UUID(export_id), settings)

    r = await client.get(f"/api/v1/exports/{export_id}/download")
    assert r.status_code == 200

    async with maker() as s:
        rows = (
            await s.execute(
                _select(SecurityEvent).where(
                    SecurityEvent.event_type == "report_export_downloaded"
                )
            )
        ).scalars().all()
    assert any((e.details or {}).get("export_id") == export_id for e in rows)
