"""P11-T8 — custom (saved-query) report CRUD + synchronous execution.

A custom report is a stored querydsl string + column projection
(:class:`filearr.models.ReportDefinition`). Every write VALIDATES by actually
parsing the query and compiling it to SQL against the CURRENT custom-field
definitions (so a bad DSL, an un-translatable filter, or an unknown column/cf is
rejected at create/update time, not at run time). ``/run`` executes through the
SAME streaming machinery as canned reports (``filearr.reports.stream_report_rows``
+ the shared CSV formula-injection guard), so a custom CSV export is just as
memory-bounded and just as safe.

Scopes mirror ``saved_searches``: GET = ``read``; POST/PATCH/DELETE + the run
endpoint = ``write`` (reporting is data-exfiltration-shaped; tightens to the
``download`` action under Phase-6 RBAC, P11-T10). ``owner_principal`` is the
nullable R7 placeholder.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.api.reports import _json_page, export_response, xlsx_response
from filearr.custom_fields import CustomFieldDef, def_from_model
from filearr.custom_reports import (
    CORE_COLUMNS,
    ColumnError,
    build_custom_report,
)
from filearr.db import get_session
from filearr.models import CustomField, ReportDefinition
from filearr.query_sql import QueryTranslationError
from filearr.querydsl import ParseError
from filearr.reports import (
    ALL_FORMATS,
    EXPORT_FORMATS,
    MAX_LIMIT,
    XLSX_FORMAT,
    CannedReport,
    ReportParams,
)
from filearr.schemas_reports import (
    ReportDefinitionIn,
    ReportDefinitionOut,
    ReportDefinitionUpdate,
    ReportValidateIn,
)
from filearr.security import PermissionContext, require_permission, require_scope

router = APIRouter()

DEFAULT_LIMIT = 1000


async def _load_custom_defs(session: AsyncSession) -> dict[str, CustomFieldDef]:
    rows = (await session.execute(select(CustomField))).scalars().all()
    return {r.name: def_from_model(r) for r in rows}


def _compile(
    query: str, columns: list[str], sort: str | None, defs: dict[str, CustomFieldDef]
) -> tuple[CannedReport | None, list[dict]]:
    """Parse + translate + validate columns; return (report|None, errors).

    Errors are structured (machine-readable ``error`` tag) so the UI can point at
    a parse position or name the offending column."""
    try:
        report = build_custom_report(
            report_id="preview",
            name="preview",
            query=query,
            columns=columns,
            sort=sort,
            custom_defs=defs,
        )
    except ParseError as exc:
        return None, [
            {
                "error": "parse_error",
                "code": exc.code,
                "position": exc.position,
                "reason": exc.reason,
            }
        ]
    except QueryTranslationError as exc:
        return None, [
            {
                "error": "translation_error",
                "message": exc.message,
                "unsupported": exc.unsupported,
            }
        ]
    except ColumnError as exc:
        return None, [{"error": "column_error", "message": str(exc)}]
    return report, []


def _require_valid(
    query: str, columns: list[str], sort: str | None, defs: dict[str, CustomFieldDef]
) -> CannedReport:
    report, errors = _compile(query, columns, sort, defs)
    if errors:
        raise HTTPException(422, {"validation": errors})
    assert report is not None
    return report


# --------------------------------------------------------------------------- #
# Column registry (drives the UI multi-select)                                #
# --------------------------------------------------------------------------- #
@router.get("/columns", dependencies=[Depends(require_scope("read"))])
async def get_columns(session: AsyncSession = Depends(get_session)) -> dict:
    """The column registry: core columns + the registered custom-field names
    (offered as ``cf.<name>``). ``meta.<key>`` is open-ended (free entry)."""
    defs = await _load_custom_defs(session)
    return {
        "core": sorted(CORE_COLUMNS),
        "custom_fields": sorted(defs),
        "formats": ["csv", "json", "ndjson", "xml"],
    }


@router.post("/validate", dependencies=[Depends(require_scope("write"))])
async def validate_report(
    body: ReportValidateIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """Dry-run validation for the live UI validator (never persists)."""
    defs = await _load_custom_defs(session)
    _report, errors = _compile(body.query, body.columns, body.sort, defs)
    return {"ok": not errors, "errors": errors}


# --------------------------------------------------------------------------- #
# CRUD                                                                        #
# --------------------------------------------------------------------------- #
@router.get(
    "",
    response_model=list[ReportDefinitionOut],
    dependencies=[Depends(require_scope("read"))],
)
async def list_custom_reports(
    owner_principal: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ReportDefinition]:
    stmt = select(ReportDefinition).order_by(ReportDefinition.name)
    if owner_principal is not None:
        stmt = stmt.where(ReportDefinition.owner_principal == owner_principal)
    return list((await session.execute(stmt)).scalars().all())


@router.get(
    "/{report_id}",
    response_model=ReportDefinitionOut,
    dependencies=[Depends(require_scope("read"))],
)
async def get_custom_report(
    report_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> ReportDefinition:
    row = await session.get(ReportDefinition, report_id)
    if row is None:
        raise HTTPException(404, "custom report not found")
    return row


@router.post(
    "",
    response_model=ReportDefinitionOut,
    status_code=201,
    dependencies=[Depends(require_scope("write"))],
)
async def create_custom_report(
    body: ReportDefinitionIn, session: AsyncSession = Depends(get_session)
) -> ReportDefinition:
    defs = await _load_custom_defs(session)
    _require_valid(body.query, body.columns, body.sort, defs)
    # Postgres treats each NULL owner as distinct, so the UNIQUE(owner, name) index
    # does NOT block two anonymous saves of the same name — enforce it in the app
    # (mirrors saved_searches) so a duplicate is a friendly 409.
    dup = (
        await session.execute(
            select(ReportDefinition).where(
                ReportDefinition.owner_principal == body.owner_principal,
                ReportDefinition.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"a report named {body.name!r} already exists")
    row = ReportDefinition(
        name=body.name,
        owner_principal=body.owner_principal,
        query=body.query,
        columns=list(body.columns),
        sort=body.sort,
        format=body.format,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, f"a report named {body.name!r} already exists") from None
    await session.refresh(row)
    return row


@router.patch(
    "/{report_id}",
    response_model=ReportDefinitionOut,
    dependencies=[Depends(require_scope("write"))],
)
async def update_custom_report(
    report_id: uuid.UUID,
    body: ReportDefinitionUpdate,
    session: AsyncSession = Depends(get_session),
) -> ReportDefinition:
    row = await session.get(ReportDefinition, report_id)
    if row is None:
        raise HTTPException(404, "custom report not found")
    fields = body.model_dump(exclude_unset=True)
    # Re-validate the RESULTING (query, columns, sort) triple against current defs.
    defs = await _load_custom_defs(session)
    _require_valid(
        fields.get("query", row.query),
        fields.get("columns", list(row.columns)),
        fields.get("sort", row.sort),
        defs,
    )
    for k, v in fields.items():
        setattr(row, k, v)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "a report with that name already exists") from None
    await session.refresh(row)
    return row


@router.delete(
    "/{report_id}", status_code=204, dependencies=[Depends(require_scope("write"))]
)
async def delete_custom_report(
    report_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    result = await session.execute(
        sa_delete(ReportDefinition).where(ReportDefinition.id == report_id)
    )
    if result.rowcount == 0:
        raise HTTPException(404, "custom report not found")
    await session.commit()


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/{report_id}/run")
async def run_custom_report(
    report_id: uuid.UUID,
    ctx: PermissionContext = Depends(require_permission("search_metadata", coarse="write")),
    format: str = Query(
        "json",
        description=(
            "Output format. `json` = paginated envelope; `csv`/`ndjson`/`xml` = "
            "streaming machine-readable exports honouring `limit` as an optional "
            "row cap (omit for the whole result)."
        ),
    ),
    limit: int | None = None,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ReportDefinition, report_id)
    if row is None:
        raise HTTPException(404, "custom report not found")
    if format not in ALL_FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(ALL_FORMATS)}")
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise HTTPException(422, "offset must be >= 0")

    defs = await _load_custom_defs(session)
    report, errors = _compile(row.query, list(row.columns), row.sort, defs)
    if errors:
        # The stored definition no longer compiles (e.g. a custom field was
        # dropped after it was saved). Surface it structured, not a 500.
        raise HTTPException(422, {"validation": errors})
    # Give the runnable report a stable id/title from the persisted row (keep the
    # per-item row_link so the UI can open each row's ItemDetail modal).
    report = CannedReport(
        id=f"custom-{row.id}",
        title=row.name,
        description="",
        columns=report.columns,
        build=report.build,
        row=report.row,
        row_link=report.row_link,
    )

    # P11-T10 action split: an EXPORT (csv/ndjson/xml/xlsx) requires the
    # `download` action + download-scoped rows; the JSON page (screen-viewing)
    # stays on the visibility scope. Unrestricted => no-op (legacy).
    if format in EXPORT_FORMATS:
        ctx.require_capability("download")
        scope_clause = ctx.sql_clause(action="download")
        # Streaming exports are full-result, capped only when the caller passes
        # ``limit`` (unlike JSON, where it is the page size).
        params = ReportParams(limit=DEFAULT_LIMIT)
        if format == XLSX_FORMAT:
            return xlsx_response(
                report, params, limit, filename_id=f"custom-{row.id}",
                scope_clause=scope_clause,
            )
        return export_response(
            report, params, format, limit, filename_id=f"custom-{row.id}",
            scope_clause=scope_clause,
        )

    scope_clause = ctx.sql_clause()
    eff_limit = DEFAULT_LIMIT if limit is None else limit
    params = ReportParams(limit=eff_limit)
    rows, has_more = await _json_page(
        session, report, params, eff_limit, offset, scope_clause=scope_clause
    )
    return {
        "report": {"id": str(row.id), "name": row.name, "columns": list(report.columns)},
        "columns": list(report.columns),
        "rows": rows,
        "limit": eff_limit,
        "offset": offset,
        "count": len(rows),
        "has_more": has_more,
    }


@router.post("/{report_id}/export", status_code=202)
async def enqueue_custom_report_export(
    report_id: uuid.UUID,
    format: str = Query("csv", description="Export format (csv/ndjson/xml/xlsx)."),
    limit: int | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("download", coarse="write")),
) -> dict:
    """Queue a BACKGROUND export of a custom report (P11-T5). Validates the stored
    definition still compiles, then defers a job; poll ``GET /exports/{id}`` and
    fetch ``GET /exports/{id}/download``."""
    from filearr.api.exports import _export_out, enqueue_export

    row = await session.get(ReportDefinition, report_id)
    if row is None:
        raise HTTPException(404, "custom report not found")
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    defs = await _load_custom_defs(session)
    _report, errors = _compile(row.query, list(row.columns), row.sort, defs)
    if errors:
        raise HTTPException(422, {"validation": errors})
    export = await enqueue_export(
        session, ctx, report_definition_id=row.id, fmt=format, limit=limit,
    )
    return _export_out(export)
