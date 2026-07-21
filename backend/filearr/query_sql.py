"""P11-T1/T2 — querydsl AST -> SQLAlchemy ``WHERE`` compiler.

Translates a parsed :class:`filearr.querydsl.Query` (the Phase-7 normative
grammar, plus the Phase-11 ``meta.``/``cf.`` dynamic-filter extension) into a
single SQLAlchemy ``ColumnElement`` over the typed :class:`filearr.models.Item`
columns, so custom reports (P11-T8) and any future SQL-side consumer share ONE
compiler with the CLI/web-UI grammar (R6: one grammar, one meaning).

Security posture (research §§5,7 — the load-bearing invariant of this module):

* **No ``text()`` / string-built SQL, ever.** Every predicate is composed from
  SQLAlchemy column objects and operators, so the *value* is always a bound
  parameter.
* **The JSONB path is never a bound value** — for ``meta.``/``cf.`` filters the
  key is spliced into a ``[]`` accessor CHAIN (``metadata['a']['b']``). That key
  is therefore re-validated here against the same strict allow-list charset the
  parser enforces (defense in depth: never trust that the caller parsed), and a
  ``cf.<name>`` key must resolve to a registered custom-field definition.
* **Numeric comparators cast in-DB** via ``.astext.cast(Numeric)``; a
  non-numeric operand raises :class:`QueryTranslationError` (the API maps it to
  422) rather than emitting invalid SQL.
* **Fuzzy (``~``) terms are UNSUPPORTED in SQL context** (they are a runtime
  re-rank layer, not a WHERE predicate) — the translator raises, listing them,
  rather than silently dropping the fuzziness and returning wrong rows.
"""

from __future__ import annotations

import datetime as _dt
import re

from sqlalchemy import Numeric, and_, cast, not_, or_, true
from sqlalchemy.sql.elements import ColumnElement

from filearr.custom_fields import CustomFieldDef
from filearr.models import Item
from filearr.querydsl import (
    CF_PREFIX,
    MAX_DYNAMIC_KEY_LEN,
    META_PREFIX,
    DateValue,
    DurationValue,
    Filter,
    ListValue,
    MetaValue,
    Query,
    SizeValue,
    StringValue,
    Term,
)

# Mirror of the parser's allow-list (defense in depth — the translator never
# trusts that its input was produced by the reference parser).
_DYNAMIC_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")

# LIKE/ILIKE metacharacters escaped so a catalog value (untrusted: filenames,
# tags, user query text) can never smuggle a wildcard.
_LIKE_ESCAPE = "\\"


class QueryTranslationError(Exception):
    """A query that parses but cannot be expressed as a SQL ``WHERE``.

    ``unsupported`` lists fuzzy terms that have no SQL predicate; other cases
    (unknown ``kind``/``cf.`` key, non-numeric numeric-comparator operand) carry
    only ``message``. The API layer renders this as a 422."""

    def __init__(self, message: str, *, unsupported: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.unsupported = unsupported or []


def _like_escape(v: str) -> str:
    for ch in (_LIKE_ESCAPE, "%", "_"):
        v = v.replace(ch, _LIKE_ESCAPE + ch)
    return v


def _validate_key(key: str) -> str:
    """Re-assert the dynamic-key allow-list; return the bare subkey."""
    prefix = META_PREFIX if key.startswith(META_PREFIX) else CF_PREFIX
    sub = key[len(prefix) :]
    if not sub or len(sub) > MAX_DYNAMIC_KEY_LEN or not _DYNAMIC_KEY_RE.fullmatch(sub):
        raise QueryTranslationError(f"invalid dynamic key {key!r}")
    return sub


def _jsonb_accessor(column, subkey: str):
    """Build a safe ``metadata['a']['b']`` accessor chain (never a string path)."""
    parts = subkey.split(".")
    acc = column[parts[0]]
    for p in parts[1:]:
        acc = acc[p]
    return acc


# --------------------------------------------------------------------------- #
# Free-text terms                                                             #
# --------------------------------------------------------------------------- #
_TEXT_COLUMNS = (Item.filename, Item.title, Item.rel_path)


def _term_predicate(term: Term) -> ColumnElement:
    pat = "%" + _like_escape(term.value) + "%"
    any_of = or_(*[col.ilike(pat, escape=_LIKE_ESCAPE) for col in _TEXT_COLUMNS])
    return not_(any_of) if term.negated else any_of


# --------------------------------------------------------------------------- #
# Known 8-key filters                                                          #
# --------------------------------------------------------------------------- #
def _cmp(column, op: str, value):
    if op == "=":
        return column == value
    if op == ">":
        return column > value
    if op == ">=":
        return column >= value
    if op == "<":
        return column < value
    if op == "<=":
        return column <= value
    raise QueryTranslationError(f"unsupported comparator {op!r}")


def _size_predicate(v: SizeValue) -> ColumnElement:
    if v.op == "range":
        return and_(Item.size >= v.lo, Item.size <= v.hi)
    return _cmp(Item.size, v.op, v.lo)


def _duration_predicate(column, v: DurationValue) -> ColumnElement:
    """A duration is an AGE: ``modified:<7d`` = modified within the last 7d.

    Translate the age comparator into an absolute-timestamp comparator against
    ``now() - age``.  ``<``/``<=`` (younger than) -> ``column > / >= threshold``;
    ``>``/``>=`` (older than) -> ``column < / <= threshold``; ``=`` -> within the
    window (``>= threshold``); a range ``lo..hi`` (older-ages) -> between
    ``now()-hi`` and ``now()-lo``."""
    now = _dt.datetime.now(_dt.UTC)

    def thr(seconds: int) -> _dt.datetime:
        return now - _dt.timedelta(seconds=seconds)

    if v.op == "range":
        return and_(column >= thr(v.hi), column <= thr(v.lo))
    t = thr(v.lo)
    if v.op in ("<", "<="):
        return column >= t if v.op == "<=" else column > t
    if v.op in (">", ">="):
        return column <= t if v.op == ">=" else column < t
    # "=" — "within this age window"
    return column >= t


def _date_predicate(column, v: DateValue) -> ColumnElement:
    """Absolute date at day granularity (``modified:>2026-01-01``)."""

    def d(iso: str) -> _dt.datetime:
        return _dt.datetime.fromisoformat(iso).replace(tzinfo=_dt.UTC)

    def nextday(iso: str) -> _dt.datetime:
        return d(iso) + _dt.timedelta(days=1)

    if v.op == "range":
        return and_(column >= d(v.lo), column < nextday(v.hi))
    if v.op == "=":
        return and_(column >= d(v.lo), column < nextday(v.lo))
    if v.op == ">":
        return column >= nextday(v.lo)
    if v.op == ">=":
        return column >= d(v.lo)
    if v.op == "<":
        return column < d(v.lo)
    if v.op == "<=":
        return column < nextday(v.lo)
    raise QueryTranslationError(f"unsupported date comparator {v.op!r}")


def _kind_predicate(v: StringValue) -> ColumnElement:
    # W8-B: the ``kind:`` DSL keyword is KEPT (stable, user-facing grammar) but now
    # maps to the taxonomy ``file_category`` (the successor to the removed
    # media_type). Values are validated against the seed category vocabulary
    # (``file_groups.FILE_CATEGORIES``); a runtime-added category still filters (the
    # WHERE clause just compares strings) but is not offered as a validated value.
    from filearr.file_groups import FILE_CATEGORIES

    if v.value not in FILE_CATEGORIES:
        raise QueryTranslationError(
            f"unknown kind {v.value!r}; expected one of {sorted(FILE_CATEGORIES)}"
        ) from None
    return Item.file_category == v.value


def _group_predicate(v: StringValue) -> ColumnElement:
    # W8-D: the ``group:`` DSL keyword maps to the taxonomy ``file_group`` (the finer
    # child of ``file_category``). Mirrors ``_kind_predicate``: values are validated
    # against the seed group vocabulary (``file_groups.FILE_GROUPS``); a
    # runtime-added group still filters (the WHERE just compares strings) but is not
    # offered as a validated value.
    from filearr.file_groups import FILE_GROUPS

    if v.value not in FILE_GROUPS:
        raise QueryTranslationError(
            f"unknown group {v.value!r}; expected one of {sorted(FILE_GROUPS)}"
        ) from None
    return Item.file_group == v.value


def _known_filter(f: Filter) -> ColumnElement:
    key, v = f.key, f.value
    if key == "kind":
        return _kind_predicate(v)
    if key == "group":
        return _group_predicate(v)
    if key == "ext":
        vals = v.values if isinstance(v, ListValue) else (v.value,)
        return Item.extension.in_(list(vals))
    if key == "path":
        # Prefix match on the identity column (index-served via text_pattern_ops).
        return Item.rel_path.like(_like_escape(v.value) + "%", escape=_LIKE_ESCAPE)
    if key == "tag":
        return Item.tags.any(v.value)
    if key == "hash":
        return or_(Item.quick_hash == v.value, Item.content_hash == v.value)
    if key == "size":
        return _size_predicate(v)
    if key == "modified":
        col = Item.mtime
    elif key == "created":
        col = Item.first_seen
    else:
        raise QueryTranslationError(f"unknown filter key {key!r}")
    if isinstance(v, DurationValue):
        return _duration_predicate(col, v)
    if isinstance(v, DateValue):
        return _date_predicate(col, v)
    raise QueryTranslationError(f"unexpected value for {key!r}")


# --------------------------------------------------------------------------- #
# Dynamic meta./cf. filters                                                    #
# --------------------------------------------------------------------------- #
def _numeric(value: str):
    try:
        return float(value)
    except ValueError:
        raise QueryTranslationError(
            f"comparator requires a numeric operand, got {value!r}"
        ) from None


def _meta_compare(accessor, v: MetaValue, *, numeric: bool) -> ColumnElement:
    astext = accessor.astext
    if v.op == "range":
        lo, hi = _numeric(v.lo), _numeric(v.hi)
        casted = cast(astext, Numeric)
        return and_(casted >= lo, casted <= hi)
    if v.op == "=":
        if numeric:
            return cast(astext, Numeric) == _numeric(v.lo)
        return astext == v.lo
    # ordered comparator
    if numeric:
        return _cmp(cast(astext, Numeric), v.op, _numeric(v.lo))
    return _cmp(astext, v.op, v.lo)


def _meta_predicate(f: Filter) -> ColumnElement:
    sub = _validate_key(f.key)
    v: MetaValue = f.value  # type: ignore[assignment]
    accessor = _jsonb_accessor(Item.metadata_, sub)
    # ``meta.`` operand type is unknown; comparators/range imply numeric, ``=``
    # defaults to text equality.
    numeric = v.op != "="
    return _meta_compare(accessor, v, numeric=numeric)


_CF_NUMERIC_TYPES = frozenset({"integer", "float"})


def _cf_predicate(f: Filter, defs: dict[str, CustomFieldDef]) -> ColumnElement:
    sub = _validate_key(f.key)
    if sub not in defs:
        raise QueryTranslationError(
            f"unknown custom field {sub!r}; not a registered custom_fields.name"
        )
    v: MetaValue = f.value  # type: ignore[assignment]
    accessor = _jsonb_accessor(Item.user_metadata, sub)
    numeric = defs[sub].data_type in _CF_NUMERIC_TYPES
    # a non-"=" comparator on a numeric-typed field casts; on a text field it is a
    # lexical comparison (astext), which the operand-parse keeps safe.
    # numeric-typed cf: cast for every comparator incl. ``=`` (numeric equality);
    # text/date/url/boolean cf: lexical astext comparison (ISO dates sort correctly).
    return _meta_compare(accessor, v, numeric=numeric)


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def ast_to_where(
    query: Query, custom_defs: dict[str, CustomFieldDef] | None = None
) -> ColumnElement:
    """Compile a parsed :class:`Query` into a single SQLAlchemy ``WHERE`` clause.

    ``custom_defs`` maps a custom-field ``name`` to its definition (used to type
    ``cf.<name>`` comparisons and reject unknown fields). Raises
    :class:`QueryTranslationError` for fuzzy terms (unsupported in SQL) and for
    un-translatable filters (unknown ``kind``/``cf.``, non-numeric numeric op)."""
    defs = custom_defs or {}
    preds: list[ColumnElement] = []

    fuzzy = [t.value for t in query.terms if t.fuzzy]
    if fuzzy:
        raise QueryTranslationError(
            "fuzzy (~) terms are not supported in report/SQL context: "
            + ", ".join(repr(x) for x in fuzzy),
            unsupported=fuzzy,
        )

    for term in query.terms:
        preds.append(_term_predicate(term))

    for f in query.filters:
        if f.key.startswith(META_PREFIX):
            pred = _meta_predicate(f)
        elif f.key.startswith(CF_PREFIX):
            pred = _cf_predicate(f, defs)
        else:
            pred = _known_filter(f)
        preds.append(not_(pred) if f.negated else pred)

    if not preds:
        return true()
    return and_(*preds)
