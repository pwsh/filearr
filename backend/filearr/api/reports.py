"""P11-T6 — reporting v1 API (canned reports, read scope).

* ``GET /api/v1/reports`` — the canned-report registry (metadata only).
* ``GET /api/v1/reports/{id}`` — run one report, either as a paginated JSON page
  (``format=json``, default) or a **streaming** machine-readable export
  (``format=csv|ndjson|xml``).

This endpoint IS the integration surface: the three streaming formats are the
supported way to pull a full report result into another tool. Each streams off a
server-side Postgres cursor (:func:`filearr.reports.stream_report_rows` +
:func:`filearr.reports.render_rows`) so a multi-hundred-thousand-row export peaks
at ~one row of memory (research §6.2). JSON stays the paginated UI envelope
(``limit``/``offset``/``has_more``); the streaming formats are full-result and
honour an OPTIONAL ``limit`` as a row cap (absent = the whole result). Every CSV
cell is formula-injection-guarded (OWASP; catalog data is untrusted) and every
XML name/value is escaped.

Reporting/export is data-exfiltration-shaped, but pre-RBAC (Phase 6) the
project's scope model applies: all report endpoints require ``read``. When RBAC
lands, this tightens to the ``download`` action + path-scoped ACL (P11-T10).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import db as _db
from filearr.db import get_session
from filearr.reports import (
    ALL_FORMATS,
    EXPORT_FORMATS,
    FORMAT_CONTENT_TYPE,
    FORMAT_EXTENSION,
    MAX_LIMIT,
    XLSX_FORMAT,
    CannedReport,
    ReportParams,
    get_report,
    list_reports,
    render_rows,
    render_xlsx_to_path,
    stream_report_rows,
)
from filearr.security import PermissionContext, require_permission, require_scope

router = APIRouter()

_FORMAT_DOC = (
    "Output format. `json` = the paginated UI envelope (limit/offset/has_more). "
    "`csv`, `ndjson`, `xml` = streaming full-result machine-readable exports "
    "(the integration surface) with the correct Content-Type + download filename; "
    "these honour `limit` as an optional row cap (omit `limit` = whole result)."
)


def _resolve(report_id: str) -> CannedReport:
    report = get_report(report_id)
    if report is None:
        raise HTTPException(404, f"unknown report {report_id!r}")
    return report


def _check_common(
    report: CannedReport,
    fmt: str,
    limit: int | None,
    offset: int,
    library_id: uuid.UUID | None,
) -> None:
    """Validation shared by JSON + streaming exports."""
    if fmt not in ALL_FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(ALL_FORMATS)}")
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise HTTPException(422, "offset must be >= 0")
    if library_id is not None and not report.supports_library:
        raise HTTPException(422, f"report {report.id!r} does not support library_id")


def _stream_params(
    report: CannedReport, limit: int | None, library_id: uuid.UUID | None
) -> tuple[ReportParams, int | None]:
    """Streaming-export (params, cap). For a CAPPED report ``limit`` is the
    definitional top-N (applied inside the cursor) so the outer cap is ``None``;
    otherwise a full stream, capped only when the caller passed ``limit``."""
    if report.is_capped:
        eff = report.default_limit if limit is None else limit
        return ReportParams(library_id=library_id, limit=eff), None
    return ReportParams(library_id=library_id, limit=report.default_limit), limit


@router.get("", dependencies=[Depends(require_scope("read"))])
async def get_reports() -> dict:
    """The canned-report registry (no query executed)."""
    return {"reports": list_reports()}


@router.get("/{report_id}")
async def run_report(
    report_id: str,
    format: str = Query("json", description=_FORMAT_DOC),
    limit: int | None = None,
    offset: int = 0,
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
):
    report = _resolve(report_id)
    _check_common(report, format, limit, offset, library_id)

    # P11-T10 action split: a machine-readable EXPORT (csv/ndjson/xml/xlsx) is
    # data-exfiltration-shaped and requires the `download` action; the paginated
    # JSON page (screen-viewing) stays on `search_metadata`. For a scoped
    # principal an export additionally scopes rows to what they may DOWNLOAD (not
    # merely view). Unrestricted (admin/API key/auth-off) => no-op, legacy.
    if format in EXPORT_FORMATS:
        ctx.require_capability("download")
        scope_clause = ctx.sql_clause(action="download")
        params, cap = _stream_params(report, limit, library_id)
        if format == XLSX_FORMAT:
            return xlsx_response(report, params, cap, scope_clause=scope_clause)
        return export_response(report, params, format, cap, scope_clause=scope_clause)

    # JSON page: visibility scope (a denied row never appears).
    scope_clause = ctx.sql_clause()
    eff_limit = report.default_limit if limit is None else limit
    params = ReportParams(library_id=library_id, limit=eff_limit)
    rows, has_more = await _json_page(
        session, report, params, eff_limit, offset, scope_clause=scope_clause
    )
    return {
        "report": report.meta(),
        "columns": list(report.columns),
        "rows": rows,
        "limit": eff_limit,
        "offset": offset,
        "count": len(rows),
        "has_more": has_more,
    }


async def _json_page(
    session: AsyncSession,
    report: CannedReport,
    params: ReportParams,
    limit: int,
    offset: int,
    scope_clause=None,
) -> tuple[list[dict], bool]:
    """One page of rows + a has-more flag.

    For a report with a Python ``post_filter`` (the scored heuristic) the SQL
    offset/limit cannot be trusted (they count pre-filter rows), so we page
    through the streaming cursor, skipping/taking in Python — memory stays
    bounded by the page window, not the full candidate set. Simple reports page
    directly in SQL (index-served ``LIMIT/OFFSET``)."""
    if report.post_filter is not None:
        rows: list[dict] = []
        idx = 0
        has_more = False
        async for d in stream_report_rows(session, report, params, scope_clause):
            if idx < offset:
                idx += 1
                continue
            if len(rows) < limit:
                rows.append(d)
                idx += 1
            else:
                has_more = True
                break
        return rows, has_more

    stmt = report.build(params)
    if scope_clause is not None:
        stmt = stmt.where(scope_clause)
    stmt = stmt.offset(offset).limit(limit + 1)
    result = await session.execute(stmt)
    fetched = [report.row(r) for r in result.all()]
    has_more = len(fetched) > limit
    return fetched[:limit], has_more


def export_response(
    report: CannedReport,
    params: ReportParams,
    fmt: str,
    cap: int | None,
    *,
    filename_id: str | None = None,
    scope_clause=None,
) -> StreamingResponse:
    """Stream a report as ``csv``/``ndjson``/``xml`` off a dedicated cursor.

    ``cap`` bounds the total rows streamed (``None`` = the whole result). A
    ``StreamingResponse`` body outlives the request-scoped ``Depends(get_session)``
    (which may already be closed when the generator runs), so we open our own
    session for the cursor."""
    columns = list(report.columns)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    fid = filename_id or report.id
    filename = f"filearr-{fid}-{stamp}.{FORMAT_EXTENSION[fmt]}"
    generated = datetime.now(UTC).isoformat()

    async def _rows():
        async with _db.SessionLocal() as session:
            n = 0
            async for d in stream_report_rows(session, report, params, scope_clause):
                if cap is not None and n >= cap:
                    break
                n += 1
                yield d

    body = render_rows(fmt, columns, _rows(), report_id=report.id, generated=generated)
    return StreamingResponse(
        body,
        media_type=FORMAT_CONTENT_TYPE[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def xlsx_response(
    report: CannedReport,
    params: ReportParams,
    cap: int | None,
    *,
    filename_id: str | None = None,
    scope_clause=None,
) -> StreamingResponse:
    """Stream a report as an ``.xlsx`` workbook (P11-T4 remainder).

    An xlsx is a zip whose central directory trails the data, so it cannot be
    produced row-by-row into the response — it is assembled to a diskguarded temp
    file with ``xlsxwriter`` ``constant_memory=True`` (peak memory ~one row, the
    same bound as the text formats) and then streamed back. Every cell is written
    as a literal string with ``strings_to_formulas`` off, so a catalog value like
    ``=SUM(A1)`` is never evaluated (the xlsx formula-injection guard). The temp
    file is removed after the body is fully sent."""
    import os
    import tempfile

    from filearr import diskguard
    from filearr.config import get_settings

    columns = list(report.columns)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    fid = filename_id or report.id
    filename = f"filearr-{fid}-{stamp}.{FORMAT_EXTENSION[XLSX_FORMAT]}"
    tmpdir = tempfile.gettempdir()
    settings = get_settings()
    diskguard.guard_write(tmpdir, settings)  # FIX-11 fail-closed pre-write
    fd, tmp_path = tempfile.mkstemp(prefix="filearr-xlsx-", suffix=".xlsx", dir=tmpdir)
    os.close(fd)

    async def _rows():
        async with _db.SessionLocal() as session:
            async for d in stream_report_rows(session, report, params, scope_clause):
                yield d

    async def _body():
        try:
            await render_xlsx_to_path(columns, _rows(), tmp_path, cap=cap)
            with open(tmp_path, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        _body(),
        media_type=FORMAT_CONTENT_TYPE[XLSX_FORMAT],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_response(report: CannedReport, params: ReportParams) -> StreamingResponse:
    """Back-compat shim: stream a report as CSV. Capped reports honour their
    top-N ``limit`` (applied in the cursor); others stream the full result."""
    cap = None
    return export_response(report, params, "csv", cap)


@router.post("/{report_id}/export", status_code=202)
async def enqueue_report_export(
    report_id: str,
    format: str = Query("csv", description="Export format (csv/ndjson/xml/xlsx)."),
    limit: int | None = None,
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("download")),
) -> dict:
    """Queue a BACKGROUND export of a canned report (P11-T5). Returns the created
    ``report_exports`` row; poll ``GET /exports/{id}`` then fetch
    ``GET /exports/{id}/download``. The sync ``GET /reports/{id}?format=...`` stays
    for smaller interactive exports."""
    from filearr.api.exports import _export_out, enqueue_export

    report = _resolve(report_id)
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    if library_id is not None and not report.supports_library:
        raise HTTPException(422, f"report {report.id!r} does not support library_id")
    export = await enqueue_export(
        session, ctx, canned_report_key=report.id, fmt=format,
        library_id=library_id, limit=limit,
    )
    return _export_out(export)
