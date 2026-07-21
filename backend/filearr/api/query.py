"""Visual filter-builder support API (user-requested filter builder page).

Two small read-scope endpoints that back the ``#/filter-builder`` UI. Neither
invents a new execution or grammar path:

* ``POST /query/preview`` — a LIVE PREVIEW of a querydsl string against real data.
  It reuses the EXACT custom-report machinery (parse -> translate -> scoped SELECT
  over ``items``): :func:`filearr.custom_reports.build_custom_report` with a fixed,
  sensible column set, run through the SAME ``_json_page`` pager + ``ctx.sql_clause``
  RBAC scoping the reports endpoint uses. So a preview is byte-for-byte the report
  execution — there is deliberately NO second query path. Fuzzy (``~``) terms
  return the identical structured ``translation_error`` the reports path gives; the
  builder simply never offers fuzzy controls.

* ``GET /query/keys`` — the value-picker vocabulary for the ``meta.<key>`` /
  ``cf.<name>`` / ``kind:`` controls. ``meta`` keys come from the code-shipped,
  per-``MediaType`` metadata profiles (:data:`filearr.profiles.METADATA_PROFILES`
  — a cheap, curated source, no ``jsonb_object_keys`` scan over the corpus); ``cf``
  names + types come from the ``custom_fields`` table; ``kind`` from the
  :class:`filearr.models.MediaType` enum.

Scoping mirrors reports exactly: ``search_metadata`` action (coarse ``read``), and
the preview rows are filtered by the caller's RBAC ``sql_clause`` so a denied item
never appears in the preview NOR the (capped) total count.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.api.custom_reports import _compile, _load_custom_defs
from filearr.api.reports import _json_page
from filearr.db import get_session
from filearr.file_groups import FILE_CATEGORIES, FILE_GROUPS
from filearr.models import CustomField
from filearr.profiles import METADATA_PROFILES
from filearr.reports import ReportParams
from filearr.schemas_reports import QueryPreviewIn
from filearr.security import PermissionContext, require_permission, require_scope

router = APIRouter()

#: Fixed, sensible projection for the live preview (the ruling's column set).
#: ``item_id`` rides on every row automatically (build_custom_report adds it) so a
#: preview row opens the same ItemDetail modal as a search hit / report row.
PREVIEW_COLUMNS: list[str] = [
    "filename",
    "library",
    "rel_path",
    "file_category",
    "file_group",
    "size",
    "mtime",
]

#: Preview page cap — a live preview is a spot-check, never a bulk pull (that is
#: what saving as a custom report + its streaming export is for).
PREVIEW_MAX_LIMIT = 50

#: Total-count ceiling. We never count the whole (potentially 750k-row) match set
#: for a live keystroke-debounced preview; we count up to the ceiling + 1 and
#: report ``total_capped`` so the UI can render e.g. "10,000+ matches" cheaply.
COUNT_CAP = 10_000


@router.post("/preview")
async def preview_query(
    body: QueryPreviewIn,
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Run a querydsl string against real data and return a small, RBAC-scoped
    page of rows plus a capped match count.

    Errors are the SAME structured shape the reports ``/validate`` + ``/run`` paths
    return (parse position, translation message, fuzzy ``unsupported`` list) with a
    422, so the builder can point at the exact problem (and never offers fuzzy)."""
    if body.limit < 1 or body.limit > PREVIEW_MAX_LIMIT:
        raise HTTPException(422, f"limit must be between 1 and {PREVIEW_MAX_LIMIT}")
    if body.offset < 0:
        raise HTTPException(422, "offset must be >= 0")

    defs = await _load_custom_defs(session)
    report, errors = _compile(body.query, PREVIEW_COLUMNS, None, defs)
    if errors:
        raise HTTPException(422, {"validation": errors})
    assert report is not None

    # P6-T4 row scoping — identical to the reports path: None => admin / API key /
    # auth-off => unfiltered.
    scope_clause = ctx.sql_clause()
    params = ReportParams(limit=body.limit)
    rows, has_more = await _json_page(
        session, report, params, body.limit, body.offset, scope_clause=scope_clause
    )

    # Capped total: wrap the report's SELECT (order stripped — irrelevant to a
    # count and cheaper) in a LIMIT COUNT_CAP+1 subquery and count that. A result
    # of COUNT_CAP+1 means "at least this many" -> total_capped=True.
    base = report.build(params)
    if scope_clause is not None:
        base = base.where(scope_clause)
    capped_sub = base.order_by(None).limit(COUNT_CAP + 1).subquery()
    total = (
        await session.execute(select(func.count()).select_from(capped_sub))
    ).scalar_one()
    total_capped = total > COUNT_CAP

    return {
        "columns": PREVIEW_COLUMNS,
        "rows": rows,
        "limit": body.limit,
        "offset": body.offset,
        "count": len(rows),
        "has_more": has_more,
        "total": min(int(total), COUNT_CAP),
        "total_capped": total_capped,
    }


@router.get("/keys", dependencies=[Depends(require_scope("read"))])
async def get_query_keys(session: AsyncSession = Depends(get_session)) -> dict:
    """Value-picker vocabulary for the builder's ``meta.``/``cf.``/``kind``/``group``
    fields.

    ``meta_keys`` are aggregated from the code-shipped metadata profiles (the
    cheap, curated source — see module docstring); ``custom_fields`` from the DB;
    ``kinds`` are the taxonomy ``file_category`` keys (W8-B: the ``kind:`` DSL
    filter maps to ``file_category``, the successor to the removed media_type);
    ``groups`` are the finer ``file_group`` keys (W8-D: the ``group:`` DSL filter
    maps to ``file_group``).
    ``source`` records the provenance so a future switch to a live
    ``jsonb_object_keys`` sample is an explicit change."""
    # Aggregate profile FieldSpecs by name, collecting which file categories emit
    # each and a representative data_type/label (first wins; profiles are
    # consistent).
    by_name: dict[str, dict] = {}
    for file_category, specs in METADATA_PROFILES.items():
        for spec in specs:
            entry = by_name.get(spec.name)
            if entry is None:
                entry = {
                    "key": spec.name,
                    "label": spec.label,
                    "data_type": spec.data_type,
                    "file_categories": [],
                }
                by_name[spec.name] = entry
            entry["file_categories"].append(file_category)
    meta_keys = sorted(by_name.values(), key=lambda e: e["key"])

    rows = (await session.execute(select(CustomField))).scalars().all()
    custom_fields = [
        {
            "name": r.name,
            "label": r.label,
            "data_type": r.data_type,
            "select_options": list(r.select_options) if r.select_options else None,
        }
        for r in sorted(rows, key=lambda r: r.name)
    ]

    return {
        "meta_keys": meta_keys,
        "custom_fields": custom_fields,
        "kinds": sorted(FILE_CATEGORIES),
        "groups": sorted(FILE_GROUPS),
        "source": "metadata_profiles+custom_fields",
    }
