"""P11-T5/T11 — background export jobs API (list / status / download).

The sync report/custom-report run endpoints stay for interactive use (bounded by
``reports.MAX_LIMIT``); a large or scheduled export instead goes through a queued
job that streams to a diskguarded staging file and is fetched here. All endpoints
require the ``download`` RBAC action (P11-T10) — reporting/export is
data-exfiltration-shaped even though it is "just metadata".

* ``POST /reports/{id}/export`` / ``POST /custom-reports/{id}/export`` (in the
  respective routers) → :func:`enqueue_export` creates a ``report_exports`` row +
  defers the job on the dedicated ``exports`` queue.
* ``GET /exports`` — the caller's own exports (an unrestricted admin / API key
  sees all).
* ``GET /exports/{id}`` — one export's status.
* ``GET /exports/{id}/download`` — re-checks RBAC **at fetch time** (not just at
  enqueue) then streams the artifact; 404 if unknown/not-owned, 409 while still
  running, 410 once purged/expired.
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import ReportExport
from filearr.reports import EXPORT_FORMATS, FORMAT_CONTENT_TYPE
from filearr.security import PermissionContext, require_permission

#: In-flight export statuses that count against the per-principal cap (T11).
_ACTIVE_EXPORT_STATUSES: tuple[str, ...] = ("queued", "running")

router = APIRouter()


def _owner_of(ctx: PermissionContext) -> str | None:
    """The stored owner principal for an enqueue (None => unrestricted actor)."""
    principal = getattr(ctx, "principal", None)
    return str(principal.id) if principal is not None else None


async def enqueue_export(
    session: AsyncSession,
    ctx: PermissionContext,
    *,
    canned_report_key: str | None = None,
    report_definition_id: uuid.UUID | None = None,
    fmt: str,
    library_id: uuid.UUID | None = None,
    limit: int | None = None,
    triggered_by: str = "manual",
) -> ReportExport:
    """Create a queued ``report_exports`` row + defer its job. The caller's RBAC
    download scope is snapshotted into ``params`` so the background job filters to
    the rows the principal may download (P11-T10)."""
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(EXPORT_FORMATS)}")
    ctx.require_capability("download")
    # P11-T11 per-principal concurrency cap: a scoped principal may hold at most
    # ``export_max_active`` in-flight (queued/running) manual exports; beyond that
    # a new enqueue is refused with 429 so one caller cannot flood the dedicated
    # `exports` queue. An unrestricted actor (admin / API key / auth-off) is exempt,
    # and schedule-triggered runs bypass this path entirely.
    owner = _owner_of(ctx)
    if not ctx.unrestricted and owner is not None:
        cap = get_settings().export_max_active
        active = (
            await session.execute(
                select(func.count())
                .select_from(ReportExport)
                .where(
                    ReportExport.owner_principal == owner,
                    ReportExport.status.in_(_ACTIVE_EXPORT_STATUSES),
                )
            )
        ).scalar_one()
        if active >= cap:
            raise HTTPException(
                429,
                f"too many active exports ({active}); wait for one to finish "
                f"(limit {cap})",
            )
    params: dict = {}
    if library_id is not None:
        params["library_id"] = str(library_id)
    if limit is not None:
        params["limit"] = int(limit)
    snap = ctx.scope_snapshot()
    if snap is not None:
        params["scope"] = snap
    export = ReportExport(
        canned_report_key=canned_report_key,
        report_definition_id=report_definition_id,
        triggered_by=triggered_by,
        owner_principal=_owner_of(ctx),
        format=fmt,
        params=params,
        status="queued",
    )
    session.add(export)
    await session.commit()
    await session.refresh(export)

    from filearr.tasks.reports import defer_export_job

    await defer_export_job(export.id)
    return export


def _export_out(ex: ReportExport) -> dict:
    return {
        "id": str(ex.id),
        "status": ex.status,
        "format": ex.format,
        "canned_report_key": ex.canned_report_key,
        "report_definition_id": (
            str(ex.report_definition_id) if ex.report_definition_id else None
        ),
        "triggered_by": ex.triggered_by,
        "row_count": ex.row_count,
        "file_size_bytes": ex.file_size_bytes,
        "error": ex.error,
        "delivery_status": ex.delivery_status,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
        "expires_at": ex.expires_at.isoformat() if ex.expires_at else None,
        "purged_at": ex.purged_at.isoformat() if ex.purged_at else None,
        "downloadable": ex.status == "complete" and ex.purged_at is None,
    }


@router.get("")
async def list_exports(
    ctx: PermissionContext = Depends(require_permission("download")),
    session: AsyncSession = Depends(get_session),
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The caller's own exports, newest first. An unrestricted actor (admin / API
    key / auth-off) sees ALL exports; a scoped principal sees only its own."""
    limit = max(1, min(500, limit))
    stmt = select(ReportExport).order_by(ReportExport.created_at.desc())
    if not ctx.unrestricted:
        stmt = stmt.where(ReportExport.owner_principal == _owner_of(ctx))
    stmt = stmt.offset(max(0, offset)).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return {"exports": [_export_out(r) for r in rows]}


async def _load_owned(
    export_id: uuid.UUID, ctx: PermissionContext, session: AsyncSession
) -> ReportExport:
    ex = await session.get(ReportExport, export_id)
    # 404 (never leak existence) when unknown or not owned by a scoped principal.
    if ex is None:
        raise HTTPException(404, "export not found")
    if not ctx.unrestricted and ex.owner_principal != _owner_of(ctx):
        raise HTTPException(404, "export not found")
    return ex


@router.get("/{export_id}")
async def get_export(
    export_id: uuid.UUID,
    ctx: PermissionContext = Depends(require_permission("download")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return _export_out(await _load_owned(export_id, ctx, session))


@router.get("/{export_id}/download")
async def download_export(
    export_id: uuid.UUID,
    request: Request,
    ctx: PermissionContext = Depends(require_permission("download")),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Stream a completed export artifact. RBAC is RE-CHECKED here (not just at
    enqueue): the ``download`` action + capability gate + ownership all apply at
    fetch time, so a principal whose download grant was revoked after enqueue can
    no longer pull the file. The served download is audited UNCONDITIONALLY
    (regardless of ``FILEARR_AUDIT_READS``): a report export is
    data-exfiltration-shaped, mirroring the transfer-download carve-out (R2)."""
    ctx.require_capability("download")  # T10 re-check at download time
    ex = await _load_owned(export_id, ctx, session)
    if ex.status == "failed":
        raise HTTPException(409, f"export failed: {ex.error or 'unknown error'}")
    if ex.status in ("queued", "running"):
        raise HTTPException(409, "export not ready")
    if ex.purged_at is not None or not ex.artifact_path:
        raise HTTPException(410, "export artifact expired")
    if not os.path.exists(ex.artifact_path):
        raise HTTPException(410, "export artifact missing")
    await audit.emit(
        audit.REPORT_EXPORT_DOWNLOADED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "export_id": str(ex.id),
            "format": ex.format,
            "canned_report_key": ex.canned_report_key,
            "report_definition_id": (
                str(ex.report_definition_id) if ex.report_definition_id else None
            ),
            "row_count": ex.row_count,
            "file_size_bytes": ex.file_size_bytes,
        },
    )
    media = FORMAT_CONTENT_TYPE.get(ex.format, "application/octet-stream")
    filename = f"filearr-export-{ex.id}.{ex.format}"
    return FileResponse(ex.artifact_path, media_type=media, filename=filename)
