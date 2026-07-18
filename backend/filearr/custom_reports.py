"""P11-T3/T8 — custom-report column registry, projection serializer, and the
bridge that turns a stored :class:`ReportDefinition` into a runnable
:class:`filearr.reports.CannedReport`.

Custom reports reuse the canned streaming/CSV machinery verbatim
(:func:`filearr.reports.stream_report_rows`): the only new pieces are (1) a
compiled ``WHERE`` from the querydsl string (``filearr.query_sql``) and (2) a
column projection. Per research §4 the projection is read off the ALREADY-FETCHED
row in Python via a safe dotted-path getter — never string-built JSON-path SQL;
all JSONB *SQL* access stays on the filter side (the translator), never the
column-selection side.

Column registry (validated on write):
* a **core** column (``rel_path``, ``library``, ``size``, ``mtime``, ...);
* ``meta.<key>`` -> extracted ``metadata`` value (dotted = nested);
* ``cf.<name>`` -> a registered custom field's EFFECTIVE value (user overlay),
  matching what the API/UI show; ``<name>`` must resolve to a ``custom_fields``
  definition.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select

from filearr import share_map
from filearr.custom_fields import CustomFieldDef
from filearr.models import Item, ItemStatus, Library, MediaType
from filearr.query_sql import ast_to_where
from filearr.querydsl import CF_PREFIX, MAX_DYNAMIC_KEY_LEN, META_PREFIX
from filearr.querydsl import parse as parse_query
from filearr.reports import CannedReport, join_prefix

_DYNAMIC_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")

_ACTIVE = Item.status == ItemStatus.active


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


#: Core column -> (Item attribute accessor for serialization). ``library`` is the
#: joined ``Library.name`` and handled specially in the serializer.
_CORE_SERIALIZE: dict[str, Callable[[Item], Any]] = {
    "rel_path": lambda it: it.rel_path,
    "path": lambda it: it.path,
    "filename": lambda it: it.filename,
    "extension": lambda it: it.extension,
    "size": lambda it: int(it.size) if it.size is not None else None,
    "mtime": lambda it: _iso(it.mtime),
    "media_type": lambda it: it.media_type.value
    if isinstance(it.media_type, MediaType)
    else str(it.media_type),
    "status": lambda it: it.status.value
    if isinstance(it.status, ItemStatus)
    else str(it.status),
    "tags": lambda it: list(it.tags or []),
    "title": lambda it: it.title,
    "year": lambda it: it.year,
    "first_seen": lambda it: _iso(it.first_seen),
    "last_seen": lambda it: _iso(it.last_seen),
    "content_hash": lambda it: it.content_hash,
    "quick_hash": lambda it: it.quick_hash,
}

#: Core column -> the SQL column used for ``sort`` (``library`` -> Library.name).
_CORE_SORT_COLUMN = {
    "rel_path": Item.rel_path,
    "path": Item.path,
    "filename": Item.filename,
    "extension": Item.extension,
    "size": Item.size,
    "mtime": Item.mtime,
    "media_type": Item.media_type,
    "status": Item.status,
    "title": Item.title,
    "year": Item.year,
    "first_seen": Item.first_seen,
    "last_seen": Item.last_seen,
    "content_hash": Item.content_hash,
    "quick_hash": Item.quick_hash,
    "library": Library.name,
}

CORE_COLUMNS: frozenset[str] = frozenset(_CORE_SERIALIZE) | {
    "library",
    "native_path",
    "share_url",
    "share_unc",
}


class ColumnError(Exception):
    """A requested projection/sort column is not in the registry."""


def _valid_dynamic_subkey(sub: str) -> bool:
    return bool(sub) and len(sub) <= MAX_DYNAMIC_KEY_LEN and bool(
        _DYNAMIC_KEY_RE.fullmatch(sub)
    )


def validate_columns(
    columns: list[str], custom_defs: dict[str, CustomFieldDef]
) -> None:
    """Validate a projection column list against the registry (raise ColumnError).

    Core columns are always allowed; ``meta.<key>`` needs an allow-listed subkey;
    ``cf.<name>`` needs ``<name>`` registered in ``custom_fields``."""
    if not columns:
        raise ColumnError("at least one column is required")
    for col in columns:
        if col in CORE_COLUMNS:
            continue
        if col.startswith(META_PREFIX):
            if not _valid_dynamic_subkey(col[len(META_PREFIX) :]):
                raise ColumnError(f"invalid meta column {col!r}")
            continue
        if col.startswith(CF_PREFIX):
            name = col[len(CF_PREFIX) :]
            if not _valid_dynamic_subkey(name):
                raise ColumnError(f"invalid cf column {col!r}")
            if name not in custom_defs:
                raise ColumnError(
                    f"unknown custom field in column {col!r}; not a registered "
                    "custom_fields.name"
                )
            continue
        raise ColumnError(
            f"unknown column {col!r}; expected a core column or meta.<key>/cf.<name>"
        )


def validate_sort(sort: str | None, custom_defs: dict[str, CustomFieldDef]) -> None:
    if sort is None or sort == "":
        return
    col = sort[1:] if sort[:1] == "-" else sort
    try:
        validate_columns([col], custom_defs)
    except ColumnError as exc:
        raise ColumnError(f"invalid sort column {sort!r}: {exc}") from None


def _safe_dotted_get(bag: dict, dotted: str) -> Any:
    """Walk a dict by a dotted key (pure Python; never SQL). Missing -> None."""
    cur: Any = bag or {}
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _project_value(
    item: Item,
    library_name: str | None,
    native_prefix: str | None,
    share_prefix: str | None,
    col: str,
) -> Any:
    if col == "library":
        return library_name
    # Computed path-context columns (P11 polish): the native_prefix/share_prefix
    # join (invariant 3), matching the canned per-item reports.
    if col == "native_path":
        return join_prefix(native_prefix, item.rel_path)
    if col == "share_url":
        # OPS-T7: manual prefix wins; else deploy mount map (auto share_prefix).
        return share_map.item_share_url(share_prefix, item.path, item.rel_path)
    if col == "share_unc":
        # UI-T15: Windows-UNC counterpart (None for non-SMB / POSIX locations).
        return share_map.item_share_location(
            share_prefix, item.path, item.rel_path
        ).unc
    if col in _CORE_SERIALIZE:
        return _CORE_SERIALIZE[col](item)
    if col.startswith(META_PREFIX):
        return _safe_dotted_get(item.metadata_, col[len(META_PREFIX) :])
    if col.startswith(CF_PREFIX):
        # cf. = EFFECTIVE value (user overlay wins), matching the API/UI.
        return _safe_dotted_get(item.effective_metadata, col[len(CF_PREFIX) :])
    return None


def _sort_target(sort: str):
    desc = sort[:1] == "-"
    col = sort[1:] if desc else sort
    if col in _CORE_SORT_COLUMN:
        target = _CORE_SORT_COLUMN[col]
    elif col.startswith(META_PREFIX):
        sub = col[len(META_PREFIX) :].split(".")
        acc = Item.metadata_[sub[0]]
        for p in sub[1:]:
            acc = acc[p]
        target = acc.astext
    elif col.startswith(CF_PREFIX):
        sub = col[len(CF_PREFIX) :].split(".")
        acc = Item.user_metadata[sub[0]]
        for p in sub[1:]:
            acc = acc[p]
        target = acc.astext
    else:  # pragma: no cover - validated earlier
        target = Item.rel_path
    return target.desc() if desc else target.asc()


def build_custom_report(
    *,
    report_id: str,
    name: str,
    query: str,
    columns: list[str],
    sort: str | None,
    custom_defs: dict[str, CustomFieldDef],
) -> CannedReport:
    """Compile a stored definition into a runnable :class:`CannedReport`.

    Raises :class:`filearr.querydsl.ParseError` (bad DSL) or
    :class:`filearr.query_sql.QueryTranslationError` (un-translatable) or
    :class:`ColumnError` (bad projection/sort) — the API maps each to a 422."""
    validate_columns(columns, custom_defs)
    validate_sort(sort, custom_defs)
    ast = parse_query(query)
    where = ast_to_where(ast, custom_defs)
    cols = tuple(columns)

    def _build(_params) -> Select:
        stmt = (
            select(
                Item,
                Library.name.label("library"),
                Library.native_prefix.label("native_prefix"),
                Library.share_prefix.label("share_prefix"),
            )
            .join(Library, Item.library_id == Library.id)
            .where(_ACTIVE, where)
        )
        if sort:
            stmt = stmt.order_by(_sort_target(sort))
        else:
            stmt = stmt.order_by(Item.rel_path.asc())
        return stmt

    def _row(r: Any) -> dict:
        item = r.Item
        # item_id rides in every custom row (JSON/NDJSON/XML) so the UI can open
        # the ItemDetail modal per row; it is NOT in ``cols`` so a CSV export
        # keeps exactly the requested projection (P11 polish).
        out: dict = {"item_id": str(item.id)}
        for c in cols:
            out[c] = _project_value(
                item, r.library, r.native_prefix, r.share_prefix, c
            )
        return out

    return CannedReport(
        id=report_id,
        title=name,
        description="",
        columns=cols,
        build=_build,
        row=_row,
        row_link="item",
    )
