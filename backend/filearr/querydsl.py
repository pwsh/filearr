"""Filearr local query DSL — the **normative reference parser** (Phase 7,
roadmap §2 / ``docs/research/phase-7-local-query-access.md`` §1.4 + §4).

**Inert scaffolding / reference implementation.** Nothing in the runtime imports
this module yet — only its tests do. It is the *single source of truth* for the
local query grammar that the offline agent's ``filearr query`` CLI and local web
UI parse (``agent/internal/localapi``, see ``agent/docs/layout.md``). Per
Architect ruling **R6**, the grammar is specified **once**, language-neutrally:
here (the Python reference) plus the canonical vectors in
``shared/querydsl-vectors.json``. The future Go port must pass those same vectors
byte-for-byte — any Python/Go divergence is a release blocker.

This parser is deliberately *pure*: string in, typed AST or structured
:class:`ParseError` out. No filesystem, no SQLite, no network, no
typo-tolerance (that is a runtime re-rank layer, brief §4.2, not part of the
grammar). Malformed input always raises :class:`ParseError` (carrying a stable
``code`` + character ``position`` + human ``reason``) — no other exception type
ever escapes :func:`parse`.

====================================================================
GRAMMAR (NORMATIVE)
====================================================================

A query is whitespace-separated **tokens**. Whitespace inside a double-quoted
span does not split. Each token is either a *filter* or a *free-text term*::

    query        := token (WS+ token)*
    token        := [ neg ] [ fuzzy ] ( filter | term )
    neg          := "-" | "!"                 # negate this token
    fuzzy        := "~"                        # explicit-fuzzy, TERMS ONLY
    filter       := KEY ":" value             # KEY is one of the known keys
    term         := WORD | '"' ... '"'         # bare or quoted free text

Recognised filter keys (anything else before ``:`` is treated as free text, so
``http://x`` or ``note:2`` are terms, never errors)::

    kind:<str>            file-category name, lower-cased       -> string
    group:<str>           file-group name, lower-cased          -> string
    ext:<a>[;<b>...]      extension list, dot-stripped, lower   -> list
    path:<glob>           glob, kept verbatim                   -> string
    tag:<str>             tag name, verbatim                    -> string
    hash:<hex>            hex digest / prefix, lower-cased      -> string
    size:<size-pred>      byte size with comparator/range       -> size
    modified:<time-pred>  mtime with comparator/range           -> duration|date
    created:<time-pred>   ctime with comparator/range           -> duration|date

Two **dynamic** filter families (T2) target JSONB by an allow-listed key. The
value carries an optional comparator or an ``A..B`` range, exactly like ``size``,
but the operand stays a raw string (the runtime JSONB type is unknown at parse
time; the SQL translator casts per comparator)::

    meta.<key>:<meta-pred>   extracted ``metadata`` JSONB value    -> meta
    cf.<name>:<meta-pred>    registered custom-field (effective)   -> meta
    meta-pred := [ cmp ] value | value ".." value

``<key>``/``<name>`` is a strict allow-list: lowercase ``[a-z0-9_]`` segments,
dot-separated (a dotted key is a nested-accessor path in SQL — ``meta.a.b`` ->
``metadata['a']['b']``), non-empty, no leading/trailing dot, <= 64 chars. An
out-of-charset key is a ``bad_meta_key`` / ``bad_cf_key`` parse error (NEVER a
silent free-text fallthrough), because the subkey becomes part of a SQL JSONB
accessor path. Bare ``meta``/``cf`` (no dot) is still ordinary free text.

Comparators (default ``=`` when omitted); ``A..B`` is an inclusive range and
may NOT carry a comparator::

    size-pred := [ cmp ] size | size ".." size
    time-pred := [ cmp ] atom | atom ".." atom          # both atoms same kind
    cmp       := ">" | ">=" | "<" | "<=" | "="

Size literals are **binary** (1024-based), integer mantissa only::

    size      := DIGITS [ "K" | "M" | "G" | "T" ]        # case-insensitive
                 # "" =bytes, K=2^10, M=2^20, G=2^30, T=2^40

Time atoms are a relative **duration** or an ISO **date** (zero-padded)::

    atom      := DIGITS ( "s"|"m"|"h"|"d"|"w" )    # s,m,h,d,w -> seconds
               | YYYY "-" MM "-" DD                # validated calendar date

Durations normalise to seconds in the AST (so ``7d`` and ``604800s`` are the
same node) — this is what makes ``parse(str(ast)) == ast`` hold.

Negation applies to the whole token. ``~`` marks a free-text term for the
runtime fuzzy layer; ``~`` on a *filter* is a ``fuzzy_on_filter`` error.

Error codes (stable across the Python reference and the Go port):
``unterminated_quote``, ``empty_value``, ``fuzzy_on_filter``, ``bad_size_value``,
``bad_size_suffix``, ``bad_time_value``, ``bad_date``, ``bad_hash``,
``bad_range``, ``bad_meta_key``, ``bad_cf_key``.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

# --- Vocabulary -------------------------------------------------------------

KEYS = frozenset(
    {"kind", "group", "ext", "size", "modified", "created", "path", "tag", "hash"}
)
_LIST_KEYS = frozenset({"ext"})
_SIZE_KEYS = frozenset({"size"})
_TIME_KEYS = frozenset({"modified", "created"})
_HASH_KEYS = frozenset({"hash"})
# everything else in KEYS is a plain string value (kind / group / path / tag)

_LOWER_KEYS = frozenset({"kind", "group", "ext", "hash"})  # value lower-cased on parse

_SIZE_SUFFIX = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_DURATION_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")

# T2 grammar extension: dynamic JSONB filter families. ``meta.<key>`` targets
# the extracted ``metadata`` JSONB; ``cf.<name>`` targets a registered custom
# field (effective overlay). The key charset is a strict allow-list validated at
# PARSE time (security: the key becomes part of a JSONB accessor path in the SQL
# translator, never a bound value) — lowercase ``[a-z0-9_.]`` only, non-empty,
# length-capped, and it may not begin/end with a dot or contain ``..``.
META_PREFIX = "meta."
CF_PREFIX = "cf."
MAX_DYNAMIC_KEY_LEN = 64
_DYNAMIC_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")


# --- Structured error -------------------------------------------------------


class ParseError(Exception):
    """The only exception :func:`parse` raises for malformed input.

    Carries a machine-stable ``code``, the 0-based character ``position`` in the
    input where the problem was detected, and a human ``reason``. The Go port
    asserts on ``code`` + ``position`` (``reason`` is informational).
    """

    def __init__(self, position: int, code: str, reason: str) -> None:
        super().__init__(f"{code} at {position}: {reason}")
        self.position = position
        self.code = code
        self.reason = reason

    def to_dict(self) -> dict:
        return {"position": self.position, "code": self.code, "reason": self.reason}


# --- Typed filter values ----------------------------------------------------


@dataclass(frozen=True)
class StringValue:
    value: str

    def to_dict(self) -> dict:
        return {"type": "string", "value": self.value}

    def __str__(self) -> str:
        return f'"{self.value}"' if _needs_quote(self.value) else self.value


@dataclass(frozen=True)
class ListValue:
    values: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"type": "list", "values": list(self.values)}

    def __str__(self) -> str:
        return ";".join(self.values)


@dataclass(frozen=True)
class SizeValue:
    op: str  # ">" ">=" "<" "<=" "=" "range"
    lo: int  # bytes
    hi: int | None = None  # bytes, only for op == "range"

    def to_dict(self) -> dict:
        if self.op == "range":
            return {"type": "size", "op": "range", "lo": self.lo, "hi": self.hi}
        return {"type": "size", "op": self.op, "bytes": self.lo}

    def __str__(self) -> str:
        if self.op == "range":
            return f"{self.lo}..{self.hi}"
        return ("" if self.op == "=" else self.op) + str(self.lo)


@dataclass(frozen=True)
class DurationValue:
    op: str
    lo: int  # seconds
    hi: int | None = None  # seconds, only for op == "range"

    def to_dict(self) -> dict:
        if self.op == "range":
            return {"type": "duration", "op": "range", "lo": self.lo, "hi": self.hi}
        return {"type": "duration", "op": self.op, "seconds": self.lo}

    def __str__(self) -> str:
        if self.op == "range":
            return f"{self.lo}s..{self.hi}s"
        return ("" if self.op == "=" else self.op) + f"{self.lo}s"


@dataclass(frozen=True)
class DateValue:
    op: str
    lo: str  # ISO YYYY-MM-DD
    hi: str | None = None  # ISO, only for op == "range"

    def to_dict(self) -> dict:
        if self.op == "range":
            return {"type": "date", "op": "range", "lo": self.lo, "hi": self.hi}
        return {"type": "date", "op": self.op, "iso": self.lo}

    def __str__(self) -> str:
        if self.op == "range":
            return f"{self.lo}..{self.hi}"
        return ("" if self.op == "=" else self.op) + self.lo


@dataclass(frozen=True)
class MetaValue:
    """Value of a ``meta.<key>:`` / ``cf.<name>:`` filter (T2 grammar extension).

    The grammar cannot know the JSONB value's runtime type, so the operand is
    kept as a raw string; the SQL translator (``filearr.query_sql``) decides the
    cast per comparator (``=`` -> text equality by default; ``<``/``<=``/``>``/
    ``>=``/range -> numeric cast for ``meta.``, type-aware for ``cf.``). Mirrors
    :class:`SizeValue`'s op/lo/hi shape so range + comparator handling is uniform.
    """

    op: str  # "=" ">" ">=" "<" "<=" "range"
    lo: str  # raw operand text
    hi: str | None = None  # raw operand, only for op == "range"

    def to_dict(self) -> dict:
        if self.op == "range":
            return {"type": "meta", "op": "range", "lo": self.lo, "hi": self.hi}
        return {"type": "meta", "op": self.op, "value": self.lo}

    def __str__(self) -> str:
        if self.op == "range":
            return f"{self.lo}..{self.hi}"
        return ("" if self.op == "=" else self.op) + self.lo


FilterValue = (
    StringValue | ListValue | SizeValue | DurationValue | DateValue | MetaValue
)


# --- AST nodes --------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    value: str
    negated: bool = False
    fuzzy: bool = False

    def to_dict(self) -> dict:
        return {"value": self.value, "negated": self.negated, "fuzzy": self.fuzzy}

    def __str__(self) -> str:
        prefix = ("-" if self.negated else "") + ("~" if self.fuzzy else "")
        body = f'"{self.value}"' if _needs_quote(self.value) else self.value
        return prefix + body


@dataclass(frozen=True)
class Filter:
    key: str
    value: FilterValue
    negated: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "negated": self.negated, "value": self.value.to_dict()}

    def __str__(self) -> str:
        return ("-" if self.negated else "") + f"{self.key}:{self.value}"


@dataclass(frozen=True)
class Query:
    terms: tuple[Term, ...] = ()
    filters: tuple[Filter, ...] = ()

    @property
    def fuzzy(self) -> bool:
        """Query-level convenience flag: any free-text term carries ``~``."""
        return any(t.fuzzy for t in self.terms)

    def to_dict(self) -> dict:
        return {
            "terms": [t.to_dict() for t in self.terms],
            "filters": [f.to_dict() for f in self.filters],
            "fuzzy": self.fuzzy,
        }

    def __str__(self) -> str:
        # Emit filters then terms; each list keeps its own input order, so a
        # re-parse yields an equal AST (equality ignores interleaving).
        return " ".join([str(f) for f in self.filters] + [str(t) for t in self.terms])


# --- Helpers ----------------------------------------------------------------


def _needs_quote(v: str) -> bool:
    return (
        v == ""
        or any(c.isspace() for c in v)
        or ":" in v
        or v[0] in '-!~"'
    )


def _read_comparator(val: str) -> tuple[str | None, str]:
    if val[:2] in (">=", "<="):
        return val[:2], val[2:]
    if val[:1] in (">", "<", "="):
        return val[:1], val[1:]
    return None, val


def _parse_size_num(s: str, pos: int) -> int:
    if s == "":
        raise ParseError(pos, "bad_size_value", "expected a size")
    if s[-1].isalpha():
        suffix = s[-1].upper()
        numpart = s[:-1]
        if suffix not in _SIZE_SUFFIX or suffix == "":
            raise ParseError(pos, "bad_size_suffix", f"unknown size suffix {s[-1]!r}")
    else:
        suffix = ""
        numpart = s
    if numpart == "" or not numpart.isdigit():
        raise ParseError(pos, "bad_size_value", f"not an integer size: {s!r}")
    return int(numpart) * _SIZE_SUFFIX[suffix]


def _parse_size(val: str, pos: int) -> SizeValue:
    op, rest = _read_comparator(val)
    if op is None and ".." in val:
        parts = val.split("..")
        if len(parts) != 2 or parts[0] == "" or parts[1] == "":
            raise ParseError(pos, "bad_range", f"malformed size range: {val!r}")
        lo = _parse_size_num(parts[0], pos)
        hi = _parse_size_num(parts[1], pos)
        return SizeValue("range", lo, hi)
    if op is not None:
        if ".." in rest:
            raise ParseError(pos, "bad_range", "a range may not carry a comparator")
        return SizeValue(op, _parse_size_num(rest, pos))
    return SizeValue("=", _parse_size_num(val, pos))


def _parse_time_atom(s: str, pos: int) -> tuple[str, object]:
    if s == "":
        raise ParseError(pos, "bad_time_value", "expected a date or duration")
    if _DATE_RE.match(s):
        try:
            _dt.date.fromisoformat(s)
        except ValueError:
            raise ParseError(pos, "bad_date", f"not a valid calendar date: {s!r}") from None
        return "date", s
    m = _DURATION_RE.match(s)
    if m:
        return "duration", int(m.group(1)) * _DURATION_UNIT[m.group(2)]
    raise ParseError(pos, "bad_time_value", f"not a date or duration: {s!r}")


def _build_time(op: str, kind: str, val: object) -> FilterValue:
    if kind == "date":
        return DateValue(op, val)  # type: ignore[arg-type]
    return DurationValue(op, val)  # type: ignore[arg-type]


def _parse_time(val: str, pos: int) -> FilterValue:
    op, rest = _read_comparator(val)
    if op is None and ".." in val:
        parts = val.split("..")
        if len(parts) != 2 or parts[0] == "" or parts[1] == "":
            raise ParseError(pos, "bad_range", f"malformed time range: {val!r}")
        k1, v1 = _parse_time_atom(parts[0], pos)
        k2, v2 = _parse_time_atom(parts[1], pos)
        if k1 != k2:
            raise ParseError(pos, "bad_range", "a range must not mix dates and durations")
        if k1 == "date":
            return DateValue("range", v1, v2)  # type: ignore[arg-type]
        return DurationValue("range", v1, v2)  # type: ignore[arg-type]
    if op is not None:
        if ".." in rest:
            raise ParseError(pos, "bad_range", "a range may not carry a comparator")
        kind, v = _parse_time_atom(rest, pos)
        return _build_time(op, kind, v)
    kind, v = _parse_time_atom(val, pos)
    return _build_time("=", kind, v)


def _parse_meta_value(val: str, pos: int) -> MetaValue:
    """Parse a ``meta.``/``cf.`` operand: an optional comparator + raw value, or an
    inclusive ``A..B`` range. The operand text is NOT coerced here (the runtime
    type is unknown at parse time) — the SQL translator casts per comparator."""
    op, rest = _read_comparator(val)
    if op is None and ".." in val:
        parts = val.split("..")
        if len(parts) != 2 or parts[0] == "" or parts[1] == "":
            raise ParseError(pos, "bad_range", f"malformed range: {val!r}")
        return MetaValue("range", parts[0], parts[1])
    if op is not None:
        if ".." in rest:
            raise ParseError(pos, "bad_range", "a range may not carry a comparator")
        if rest == "":
            raise ParseError(pos, "empty_value", "comparator with no value")
        return MetaValue(op, rest)
    return MetaValue("=", val)


def _is_dynamic_key(key: str) -> bool:
    """True if ``key`` uses the ``meta.``/``cf.`` dynamic-filter prefix (T2)."""
    return key.startswith(META_PREFIX) or key.startswith(CF_PREFIX)


def _validate_dynamic_key(key: str, pos: int) -> None:
    """Allow-list gate for a ``meta.``/``cf.`` subkey (security: the subkey becomes
    a JSONB accessor path, never a bound value). Raises the family-specific code."""
    prefix, code = (
        (META_PREFIX, "bad_meta_key")
        if key.startswith(META_PREFIX)
        else (CF_PREFIX, "bad_cf_key")
    )
    sub = key[len(prefix) :]
    if not sub or len(sub) > MAX_DYNAMIC_KEY_LEN or not _DYNAMIC_KEY_RE.fullmatch(sub):
        raise ParseError(
            pos,
            code,
            f"invalid {prefix}key {sub!r}: use lowercase [a-z0-9_], dot-separated, "
            f"no leading/trailing dot, <= {MAX_DYNAMIC_KEY_LEN} chars",
        )


def _parse_filter_value(key: str, val: str, pos: int) -> FilterValue:
    if val == "":
        raise ParseError(pos, "empty_value", f"{key}: has an empty value")
    if _is_dynamic_key(key):
        return _parse_meta_value(val, pos)
    if key in _LOWER_KEYS:
        val = val.lower()
    if key in _LIST_KEYS:
        out: list[str] = []
        for part in val.split(";"):
            part = part.lstrip(".")
            if part == "":
                raise ParseError(pos, "empty_value", "empty item in list value")
            out.append(part)
        return ListValue(tuple(out))
    if key in _HASH_KEYS:
        if not _HEX_RE.match(val):
            raise ParseError(pos, "bad_hash", f"not a hex digest: {val!r}")
        return StringValue(val)
    if key in _SIZE_KEYS:
        return _parse_size(val, pos)
    if key in _TIME_KEYS:
        return _parse_time(val, pos)
    return StringValue(val)  # kind / group / path / tag


# --- Lexer ------------------------------------------------------------------

# A char is (character, in_quote, original_index).
_Char = tuple[str, bool, int]


def _lex(s: str) -> list[list[_Char]]:
    tokens: list[list[_Char]] = []
    cur: list[_Char] | None = None
    in_quote = False
    quote_start = -1
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_quote:
            if c == '"':
                in_quote = False
            else:
                cur.append((c, True, i))  # type: ignore[union-attr]
            i += 1
            continue
        if c == '"':
            if cur is None:
                cur = []
            in_quote = True
            quote_start = i
            i += 1
            continue
        if c.isspace():
            if cur is not None:
                tokens.append(cur)
                cur = None
            i += 1
            continue
        if cur is None:
            cur = []
        cur.append((c, False, i))
        i += 1
    if in_quote:
        raise ParseError(quote_start, "unterminated_quote", "unterminated quoted string")
    if cur is not None:
        tokens.append(cur)
    return tokens


def _parse_token(pairs: list[_Char]) -> Term | Filter | None:
    idx = 0
    negated = False
    fuzzy = False
    fuzzy_pos = -1
    if idx < len(pairs) and not pairs[idx][1] and pairs[idx][0] in "-!":
        negated = True
        idx += 1
    if idx < len(pairs) and not pairs[idx][1] and pairs[idx][0] == "~":
        fuzzy = True
        fuzzy_pos = pairs[idx][2]
        idx += 1
    rest = pairs[idx:]
    # First unquoted colon splits a filter key from its value.
    colon = -1
    for j, (c, q, _) in enumerate(rest):
        if c == ":" and not q:
            colon = j
            break
    if colon > 0:
        key_chars = rest[:colon]
        key = "".join(c for c, _, _ in key_chars)
        key_unquoted = all(not q for _, q, _ in key_chars)
        key_pos = key_chars[0][2]
        lkey = key.lower()
        # A known 8-key filter matches case-insensitively (kind:/EXT: ...). A
        # dynamic ``meta.``/``cf.`` filter is detected on the ORIGINAL case so an
        # uppercase subkey is a hard ``bad_*_key`` (strict allow-list), not a
        # silent free-text fallthrough.
        is_known = lkey in KEYS
        is_dynamic = _is_dynamic_key(key)
        if key_unquoted and (is_known or is_dynamic):
            if fuzzy:
                raise ParseError(
                    fuzzy_pos,
                    "fuzzy_on_filter",
                    "'~' fuzzy marker is only valid on free-text terms",
                )
            if is_dynamic:
                _validate_dynamic_key(key, key_pos)
            store_key = key if is_dynamic else lkey
            colon_index = rest[colon][2]
            value = "".join(c for c, _, _ in rest[colon + 1 :])
            fv = _parse_filter_value(store_key, value, colon_index + 1)
            return Filter(store_key, fv, negated)
    value = "".join(c for c, _, _ in rest)
    if value == "":
        return None
    return Term(value, negated, fuzzy)


# --- Public entry point -----------------------------------------------------


def parse(s: str) -> Query:
    """Parse a query string into a :class:`Query` AST.

    Raises :class:`ParseError` (and only that) on malformed input.
    """
    terms: list[Term] = []
    filters: list[Filter] = []
    for pairs in _lex(s):
        node = _parse_token(pairs)
        if node is None:
            continue
        if isinstance(node, Filter):
            filters.append(node)
        else:
            terms.append(node)
    return Query(tuple(terms), tuple(filters))
