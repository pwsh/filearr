"""Postgres-side RBAC scope filtering (Phase 6, P6-T4).

The **collection** counterpart to ``search_scope`` (which compiles a Meilisearch
filter) and to the per-item ``rbac.evaluate`` check: given a principal's resolved
grants, build a SQLAlchemy ``WHERE`` predicate over an ``items.path_scope`` column
that admits **exactly** the rows ``rbac.evaluate`` would allow for one ``action``
— same longest-prefix-wins / explicit-deny-wins / ceiling-clamp / default-deny
semantics — so a listing/report/export never surfaces a row the caller can't
read.

Column-type duality (P6-T2a "ltree-in-prod / text-in-sandbox"): the stored value
is the identical dotted ltree string either way, but the DB column TYPE differs —
a native ``ltree`` in production (stock ``postgres:18`` ships contrib) and a
plain ``text`` column in the pgserver test sandbox (no contrib). The two forms:

* **ltree** — ``path_scope <@ CAST(:scope AS ltree)`` (native, GiST-indexable
  descendant-or-self test: the item scope is at/under the grant scope).
* **text**  — ``path_scope = :scope OR starts_with(path_scope, :scope || '.')``
  (the identical set, expressed with the wildcard-free ``starts_with`` so no
  LIKE metacharacter — ``%`` / ``_`` (encoded ltree labels are underscore-dense)
  — can ever be misread as a pattern; the trailing dot keeps the prefix
  label-boundary-safe, so ``lib_1.movies`` never matches ``lib_1.movies_extra``).

Which form to emit is chosen at request time by :func:`path_scope_uses_ltree`
(one cached ``information_schema`` probe per engine). Both forms are proven
equivalent to ``rbac.evaluate`` by the property test
(``test_rbac_enforcement_p6t4``); the text path executes in the sandbox, the
ltree path renders-and-is-documented (its ``<@`` semantics are Postgres-native).

A ``NULL`` ``path_scope`` (an item scanned before P6-T2 stamped the column)
matches no clause → invisible to a scoped principal (fail-closed), exactly like
the Meili filter's empty ancestor array.
"""

from __future__ import annotations

import re

from sqlalchemy import and_, cast, false, func, literal, not_, or_
from sqlalchemy import text as sa_text
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType

from filearr import rbac

# --- ltree column-type detection (cached per engine) ------------------------ #
_LTREE_COLTYPE_CACHE: dict[int, bool] = {}


async def path_scope_uses_ltree(session) -> bool:
    """True when ``items.path_scope`` is a native ``ltree`` column (production),
    False when it is a ``text`` fallback (sandbox). Cached per bound engine — the
    column type never changes at runtime, so one probe per process suffices."""
    bind = session.get_bind()
    key = id(bind)
    cached = _LTREE_COLTYPE_CACHE.get(key)
    if cached is not None:
        return cached
    udt = (
        await session.execute(
            sa_text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'items' AND column_name = 'path_scope'"
            )
        )
    ).scalar_one_or_none()
    use = udt == "ltree"
    _LTREE_COLTYPE_CACHE[key] = use
    return use


class _Ltree(UserDefinedType):
    """Minimal ``ltree`` type marker so ``CAST(:scope AS ltree)`` renders without
    importing sqlalchemy-utils / a contrib dependency. Only used to emit the
    cast in the production (ltree) branch; never instantiated in the sandbox."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:  # noqa: D401
        return "ltree"


_LTREE = _Ltree()

# Encoded ltree scopes are drawn from exactly this alphabet (see
# ``rbac.encode_path_label`` / ``library_label``): lowercase hex-escaped labels
# joined by dots, plus the ``h__`` hash sentinel. Any scope that violates it is a
# bug upstream; we reject it (fail-closed) rather than risk a malformed clause.
_SCOPE_RE = re.compile(r"^[a-z0-9_.]+$")


def _valid_scope(scope: str) -> bool:
    return bool(_SCOPE_RE.match(scope))


def _covers(column: ColumnElement, scope: str, *, use_ltree: bool) -> ColumnElement:
    """Predicate: ``column`` (an item's path_scope) is at-or-under ``scope`` — the
    grant scope is an ancestor-or-self of the item, i.e. the grant covers it.

    The text branch uses ``starts_with`` (wildcard-free) rather than ``LIKE`` so
    the underscore-dense encoded labels can't be misread as LIKE patterns and so
    the literal-bound form (spliced into raw browse SQL) carries no backslash /
    percent escaping to get wrong."""
    if use_ltree:
        return column.op("<@")(cast(literal(scope), _LTREE))
    return or_(column == scope, func.starts_with(column, scope + "."))


def scope_where_clause(
    role: rbac.Role,
    grants: list[rbac.PathGrant],
    *,
    action: str,
    column: ColumnElement,
    use_ltree: bool,
) -> ColumnElement | None:
    """A SQLAlchemy WHERE predicate over ``column`` (``items.path_scope``) that
    admits exactly the rows ``rbac.evaluate(grants, role, path_scope, action)``
    would ALLOW. Returns:

    * ``None`` — unrestricted (``admin`` role): the caller injects no filter
      (byte-identical to the pre-RBAC query, mirroring ``evaluate``'s admin
      bypass).
    * a ``false()`` literal — the action exceeds the role ceiling, or the
      principal has no effective allow scope for it → matches nothing
      (fail-closed, mirroring ``ceiling_clamped`` / ``no_grant_default_deny``).
    * an ``OR`` of per-allow-scope terms otherwise. For an allow scope ``a`` with
      one or more deny scopes strictly beneath it, the term is
      ``covers(a) AND NOT (covers(d1) OR covers(d2) ...)`` — reproducing
      longest-prefix-wins + explicit-deny-wins (the deepest matching grant, which
      for any item is unique because its ancestors form a chain, decides).

    Same construction the deny-aware Meili filter uses
    (``tenant_tokens.compile_scope_filter``), transposed from the document's
    ancestor-array membership to the item's single ``path_scope`` value."""
    if role is rbac.Role.ADMIN:
        return None
    # Ceiling clamp (evaluate step 2): an action outside the role ceiling can
    # never be granted, so no row matches — even if a group grant carried it.
    if action not in rbac.ROLE_CEILINGS.get(role, frozenset()):
        return false()

    allow = {g.path for g in grants if g.action == action and g.allow}
    deny = {g.path for g in grants if g.action == action and not g.allow}
    # Deny-wins at equal specificity: a scope that is BOTH allow and deny is deny.
    allow -= deny
    for s in allow | deny:
        if not _valid_scope(s):
            # A grant scope outside the ltree alphabet is corrupt — refuse to
            # build a filter around it (fail-closed: no rows).
            return false()
    if not allow:
        return false()

    terms: list[ColumnElement] = []
    for a in sorted(allow):
        covered = _covers(column, a, use_ltree=use_ltree)
        under = [d for d in deny if d.startswith(a + ".")]
        if under:
            neg = or_(*[_covers(column, d, use_ltree=use_ltree) for d in under])
            terms.append(and_(covered, not_(neg)))
        else:
            terms.append(covered)
    return terms[0] if len(terms) == 1 else or_(*terms)


def compile_scope_fragment(clause: ColumnElement | None) -> str:
    """Render a :func:`scope_where_clause` predicate to a standalone SQL string
    (literal-bound) for splicing into a RAW ``text()`` query (e.g. the folder
    browse aggregates that are hand-written SQL, not ORM ``select``s).

    Safe against injection: every bound value is a grant scope drawn from the
    restricted ltree alphabet ``[a-z0-9_.]`` (asserted in
    :func:`scope_where_clause`), so literal binding cannot smuggle a quote or
    metacharacter. ``None`` (unrestricted) → ``""`` (caller adds no fragment);
    a ``false()`` clause → ``"false"`` (matches nothing)."""
    if clause is None:
        return ""
    from sqlalchemy.dialects import postgresql

    compiled = clause.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return str(compiled)
