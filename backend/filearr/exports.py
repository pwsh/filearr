"""P11-T5/T11 — background report/export job core (DB-aware helpers).

An async export streams a report result (canned or custom) to a diskguarded
staging file under ``{config_dir}/exports`` and records the job lifecycle on a
``report_exports`` row. This module owns the machinery; the Procrastinate task
entrypoint lives in :mod:`filearr.tasks.reports` and the HTTP surface in
:mod:`filearr.api.exports`.

Design (research §6/§7 + the standing priority order security > integrity >
reliability > speed):

* **Bounded memory always.** Text formats stream row-by-row off the same
  ``AsyncSession.stream()`` server-side cursor as the sync endpoints
  (:func:`filearr.reports.stream_report_rows` + :func:`~filearr.reports.render_rows`);
  xlsx is assembled with ``constant_memory=True`` (peak ~one row).
* **Fail-closed on disk pressure (FIX-11).** :func:`filearr.diskguard.guard_write`
  is called before opening the artifact; at the CRITICAL free-space floor it
  raises :class:`~filearr.diskguard.DiskGuardError` (``disk_full_guard`` token),
  the job goes ``failed`` and any partial file is removed.
* **Artifacts outside any web root.** The staging dir is under ``config_dir`` and
  the filename is a content-addressed blake2b hex of the export id + source +
  format (no user string in the path → no traversal), served only through the
  auth-checked download endpoint — never a bookmarkable static URL (research §7).
* **RBAC scope snapshot (P11-T10).** A scoped principal's export is filtered to
  the rows it may DOWNLOAD via a grant snapshot captured at enqueue
  (:meth:`filearr.security.PermissionContext.scope_snapshot`) and rebuilt here.
* **Lifecycle (invariant 7).** A crashed job leaves the row ``running``; the
  reconcile sweep flips it to ``failed``. Past ``expires_at`` the TTL purge
  deletes the file and stamps ``purged_at``, KEEPING the row (audit trail).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import diskguard
from filearr.errors import sanitize_error
from filearr.models import Item, ReportExport
from filearr.reports import (
    MAX_LIMIT,
    XLSX_FORMAT,
    CannedReport,
    ReportParams,
    get_report,
    render_rows,
    render_xlsx_to_path,
    stream_report_rows,
)


def export_dir(settings) -> str:
    """Staging directory for export artifacts (``{config_dir}/exports`` by
    default, or ``FILEARR_EXPORT_DIR`` when set)."""
    return settings.export_dir or os.path.join(settings.config_dir, "exports")


def artifact_name(export_id: uuid.UUID | str, source: str, fmt: str) -> str:
    """Content-addressed filename for an export artifact (no user string → no
    path traversal). blake2b over the export id + source ref + format."""
    from filearr.reports import FORMAT_EXTENSION

    digest = hashlib.blake2b(
        f"{export_id}:{source}:{fmt}".encode(), digest_size=16
    ).hexdigest()
    return f"{digest}.{FORMAT_EXTENSION.get(fmt, 'dat')}"


def scope_clause_from_snapshot(snapshot: dict | None, action: str, column=None):
    """Rebuild a SQL scope predicate from a
    :meth:`PermissionContext.scope_snapshot` (P11-T10). ``None`` → no filter."""
    if not snapshot:
        return None
    from filearr import rbac, rbac_sql

    role = rbac.Role(snapshot["role"])
    grants = [
        rbac.PathGrant(path=g["path"], action=g["action"], allow=g["allow"])
        for g in snapshot.get("grants", [])
    ]
    col = column if column is not None else Item.path_scope
    return rbac_sql.scope_where_clause(
        role, grants, action=action, column=col, use_ltree=snapshot.get("use_ltree", False)
    )


async def _resolve_report(
    session: AsyncSession, export: ReportExport
) -> tuple[CannedReport, ReportParams]:
    """Resolve an export row's source into a runnable report + params."""
    params_json = export.params or {}
    lib = params_json.get("library_id")
    library_id = uuid.UUID(lib) if lib else None
    limit = int(params_json.get("limit") or 1000)
    if export.canned_report_key is not None:
        report = get_report(export.canned_report_key)
        if report is None:
            raise ValueError(f"unknown canned report {export.canned_report_key!r}")
        return report, ReportParams(library_id=library_id, limit=limit)
    from filearr.custom_fields import def_from_model
    from filearr.custom_reports import build_custom_report
    from filearr.models import CustomField, ReportDefinition

    rd = await session.get(ReportDefinition, export.report_definition_id)
    if rd is None:
        raise ValueError("report definition not found")
    defs_rows = (await session.execute(select(CustomField))).scalars().all()
    defs = {r.name: def_from_model(r) for r in defs_rows}
    report = build_custom_report(
        report_id=f"custom-{rd.id}",
        name=rd.name,
        query=rd.query,
        columns=list(rd.columns),
        sort=rd.sort,
        custom_defs=defs,
    )
    return report, ReportParams(limit=limit)


async def run_export(session: AsyncSession, export_id: uuid.UUID, settings) -> dict:
    """Execute one queued export end to end.

    Marks the row ``running``, streams the report to a diskguarded artifact,
    stamps ``complete`` + counts/size/path/``expires_at``, and (when produced by a
    schedule) delivers it through the schedule's channel. On any failure the row
    goes ``failed`` with a sanitized ``error`` and any partial file is removed."""
    export = await session.get(ReportExport, export_id)
    if export is None:
        return {"status": "missing"}
    if export.status in ("complete", "failed"):
        return {"status": export.status}

    export.status = "running"
    export.started_at = datetime.now(UTC)
    await session.commit()

    fmt = export.format
    source = export.canned_report_key or str(export.report_definition_id)
    directory = export_dir(settings)
    path = os.path.join(directory, artifact_name(export.id, source, fmt))
    scope = scope_clause_from_snapshot(
        (export.params or {}).get("scope"), action="download"
    )
    cap = min(settings.export_max_rows, MAX_LIMIT * 100)

    try:
        os.makedirs(directory, exist_ok=True)
        diskguard.guard_write(directory, settings)  # FIX-11 fail-closed
        report, params = await _resolve_report(session, export)

        def _rows():
            return stream_report_rows(session, report, params, scope)

        if fmt == XLSX_FORMAT:
            row_count = await render_xlsx_to_path(
                list(report.columns), _rows(), path, cap=cap
            )
        else:
            row_count = await _write_text(fmt, report, _rows(), path, cap)

        size = os.path.getsize(path) if os.path.exists(path) else 0
        export.status = "complete"
        export.row_count = row_count
        export.file_size_bytes = size
        export.artifact_path = path
        export.finished_at = datetime.now(UTC)
        export.expires_at = export.finished_at + timedelta(
            hours=settings.export_ttl_hours
        )
        export.error = None
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — any failure => terminal 'failed'
        _safe_unlink(path)
        await session.rollback()
        export = await session.get(ReportExport, export_id)
        if export is not None:
            export.status = "failed"
            export.error = sanitize_error(exc)
            export.finished_at = datetime.now(UTC)
            await session.commit()
        return {"status": "failed", "error": sanitize_error(exc)}

    if export.schedule_id is not None:
        try:
            from filearr.report_delivery import deliver_scheduled_export

            await deliver_scheduled_export(session, export, settings)
        except Exception:  # noqa: BLE001 — delivery never fails the export itself
            import logging

            logging.getLogger("filearr.exports").warning(
                "scheduled export delivery raised", exc_info=True
            )
    return {"status": export.status, "rows": export.row_count}


async def _write_text(fmt, report, rows, path: str, cap: int) -> int:
    """Stream a text export (csv/ndjson/xml) to ``path`` off the shared serializer,
    counting rows. Peak memory ~one row."""
    generated = datetime.now(UTC).isoformat()
    counter = {"n": 0}

    async def _capped():
        async for d in rows:
            if counter["n"] >= cap:
                break
            counter["n"] += 1
            yield d

    with open(path, "w", encoding="utf-8", newline="") as fh:
        async for chunk in render_rows(
            fmt, list(report.columns), _capped(), report_id=report.id, generated=generated
        ):
            fh.write(chunk)
    return counter["n"]


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


async def purge_expired_exports(session: AsyncSession, settings, now=None) -> int:
    """Delete artifact files past ``expires_at`` and stamp ``purged_at`` (row
    kept for audit). Returns the number of artifacts purged (P11-T11)."""
    now = now or datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ReportExport).where(
                    ReportExport.expires_at.isnot(None),
                    ReportExport.expires_at < now,
                    ReportExport.purged_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    purged = 0
    for ex in rows:
        if ex.artifact_path:
            _safe_unlink(ex.artifact_path)
        ex.purged_at = now
        ex.artifact_path = None
        purged += 1
    if purged:
        await session.commit()
    return purged


async def reconcile_stale_exports(
    session: AsyncSession, settings, now=None, *, timeout_minutes: int = 60
) -> int:
    """Flip an export stuck ``running`` past ``timeout_minutes`` to ``failed``
    (invariant 7: a crashed job must never be left ``running``). Returns count."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=timeout_minutes)
    result = await session.execute(
        update(ReportExport)
        .where(
            ReportExport.status == "running",
            ReportExport.started_at.isnot(None),
            ReportExport.started_at < cutoff,
        )
        .values(
            status="failed",
            error="export reconciled: stale running job (worker crash?)",
            finished_at=now,
        )
    )
    if result.rowcount:
        await session.commit()
    return result.rowcount or 0
