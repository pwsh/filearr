"""Meilisearch tenant-token compilation (Phase 6, roadmap §3 §8 /
``docs/research/phase-6-identity-auth-rbac.md`` §2.6).

**Inert scaffolding.** Only the tests import this module. It ships the *pure*
grant → Meilisearch-filter compilation step (the part that needs no Meilisearch,
no signing key, no network) plus the size-ceiling guard, and a typed
``NotImplementedError`` stub for the stateful token-minting step (P6-T3).

Enforcement model (brief §2.6): a principal's allowed *search* scopes compile
into a Meilisearch filter expression embedded in a per-session signed tenant
token, so Meilisearch itself does row-level filtering at query time — the client
is never trusted. Tenant tokens gate **search visibility only**
(``search_metadata`` / ``search_content``); ``download`` / ``modify`` stay
Postgres-side checks on their own endpoints.

Architect rulings baked in (see the tasks doc):

- **R2** — filter size is measured (P6-T3), and above :data:`FILTER_SIZE_CEILING`
  compilation **REFUSES** with :class:`CompilationRefused` rather than silently
  coarsening to a broader ancestor (integrity first: silent precision loss is
  forbidden; the admin must consolidate grants). The ceiling constant here is a
  documented placeholder pending the P6-T3 measurement against real
  JWT/header-size and Meilisearch filter-complexity limits.
- **R3** — tenant tokens are signed by a **per-user** Meilisearch parent key
  (per-session is the documented escalation path). :func:`mint_tenant_token` is
  the P6-T3 stub where that signing happens.
"""

from __future__ import annotations

from dataclasses import dataclass

from filearr.rbac import Decision, PathGrant, Role

# Placeholder ceiling on the compiled filter-expression length (characters).
# R2: this is MEASURED in P6-T3 against the real Meilisearch tenant-token JWT
# payload limit / HTTP header ceiling / filter-complexity limit; the value here
# is a conservative stand-in so the refusal path exists and is tested now. Above
# it, compilation refuses (consolidate grants) — NEVER coarsen silently.
FILTER_SIZE_CEILING: int = 4096

# The filterable Meilisearch attribute each document carries (added at index
# time by index_sync per brief §2.6 step 2). Each doc's value is the set of
# ltree ancestor labels covering it, so an `IN` filter matches by scope.
PATH_SCOPE_ATTR = "path_scope"
# Boolean filterable attribute distinguishing sidecar docs (T3 sidecar_of).
_ATTR_IS_SIDECAR = "is_sidecar"

# Only these actions are enforceable via tenant tokens (search visibility).
_SEARCH_ACTIONS = frozenset({"search_metadata", "search_content"})


class CompilationRefused(Exception):
    """Raised (R2) when the compiled filter exceeds :data:`FILTER_SIZE_CEILING`.

    Carries the offending size and the ceiling so the admin-facing error can say
    "consolidate grants" with concrete numbers. Refusing is deliberate: silently
    dropping precision (coarsening many narrow grants to a broad ancestor) would
    over-share and violate the security-first / integrity-first ordering."""

    def __init__(self, size: int, ceiling: int, clause_count: int) -> None:
        self.size = size
        self.ceiling = ceiling
        self.clause_count = clause_count
        super().__init__(
            f"compiled tenant-token filter is {size} chars over {clause_count} "
            f"path scopes, exceeding the {ceiling}-char ceiling; consolidate "
            f"grants into fewer, broader path scopes"
        )


def _escape_scope(scope: str) -> str:
    """Quote a single ltree scope value for a Meilisearch filter list. ltree
    labels are ``[A-Za-z0-9_.]`` only, so no filter-syntax metacharacter can
    appear; we still wrap in double quotes for a stable, injection-proof form."""
    return f'"{scope}"'


@dataclass(frozen=True, slots=True)
class CompiledFilter:
    """Result of :func:`compile_filter`: the Meilisearch filter ``expression``
    plus the distinct ``scopes`` that produced it (for logging/debugging)."""

    expression: str
    scopes: tuple[str, ...]


def compile_filter(
    grants: list[PathGrant | Decision],
    *,
    include_sidecars: bool = True,
) -> CompiledFilter:
    """Compile allowed search scopes into a Meilisearch filter expression (PURE).

    Accepts either resolved :class:`~filearr.rbac.PathGrant` rows or the
    :class:`~filearr.rbac.Decision` objects an evaluation produced. Only *allow*
    grants for a search action contribute a scope; denies are handled upstream
    by :func:`~filearr.rbac.evaluate` (a denied subtree simply never appears as
    an allowed scope here). Produces a ``path_scope IN [...]`` expression whose
    membership Meilisearch evaluates per document.

    ``is_sidecar`` interplay (brief §2.6 / T3 ``sidecar_of``): sidecar documents
    are indexed with the ``path_scope`` of their *parent* item, so they inherit
    the parent's visibility automatically and need no separate clause. When
    ``include_sidecars`` is False the caller wants primary items only, and we
    append ``AND ${is_sidecar} = false`` — noted here as the integration point;
    the default keeps sidecars visible exactly where their parent is.

    Raises :class:`CompilationRefused` (R2) if the expression exceeds
    :data:`FILTER_SIZE_CEILING`.
    """
    scopes: list[str] = []
    seen: set[str] = set()
    for g in grants:
        grant = g.grant if isinstance(g, Decision) else g
        if grant is None or not grant.allow:
            continue
        if grant.action not in _SEARCH_ACTIONS:
            continue
        if grant.path not in seen:
            seen.add(grant.path)
            scopes.append(grant.path)

    if not scopes:
        # No allowed search scope → a filter that matches nothing (fail-closed).
        expression = f"{PATH_SCOPE_ATTR} IN []"
        if not include_sidecars:
            expression = f"{expression} AND {_ATTR_IS_SIDECAR} = false"
        _guard_size(expression, 0)
        return CompiledFilter(expression=expression, scopes=())

    ordered = tuple(sorted(scopes))
    joined = ", ".join(_escape_scope(s) for s in ordered)
    expression = f"{PATH_SCOPE_ATTR} IN [{joined}]"
    if not include_sidecars:
        expression = f"{expression} AND {_ATTR_IS_SIDECAR} = false"

    _guard_size(expression, len(ordered))
    return CompiledFilter(expression=expression, scopes=ordered)


def _guard_size(expression: str, clause_count: int) -> None:
    if len(expression) > FILTER_SIZE_CEILING:
        raise CompilationRefused(len(expression), FILTER_SIZE_CEILING, clause_count)


# --------------------------------------------------------------------------- #
# P6-T3 — deny-aware scope filter + ancestor projection (server-side proxy).    #
#                                                                               #
# The active enforcement model shipped in P6-T3 is the SERVER-SIDE PROXY: the   #
# API computes the caller's allowed-scope Meilisearch filter from their live    #
# DB grants and injects it into every Meili query (Meili does the row-level     #
# filtering — the API never post-filters hits). This is distinct from the       #
# browser-direct TENANT-TOKEN model (``mint_tenant_token`` below), which stays  #
# a stub because Filearr's browser never talks to Meili directly; per-user      #
# parent keys / token minting are the phase-9 escalation (see the tasks doc     #
# "Cross — phase-9 owns the per-user-parent-key operational model").            #
#                                                                               #
# Projection: each document carries ``path_scope`` = the ARRAY of every ltree   #
# ancestor label covering the item (library root … item leaf). A grant scope is #
# an ancestor prefix, so an equality/``IN`` test on that array matches exactly  #
# the items the grant covers — no substring/prefix operator needed (Meili has   #
# none), which is why the ancestor SET is materialised at index time.           #
# --------------------------------------------------------------------------- #


def scope_ancestors(path_scope: str) -> tuple[str, ...]:
    """Every ltree ancestor-or-self prefix of ``path_scope`` (the Meili
    ``path_scope`` array a document carries).

    ``"lib_x.movies.inception.f_mkv"`` → ``("lib_x", "lib_x.movies",
    "lib_x.movies.inception", "lib_x.movies.inception.f_mkv")``. A grant at any of
    those exact prefixes then matches this item via ``path_scope = "<prefix>"``
    (equality on an array attribute is Meili "array contains"). Empty / falsy
    input → ``()`` (an unscoped item matches no grant → fail-closed for scoped
    principals; still visible to admin / auth-off which inject no filter)."""
    if not path_scope:
        return ()
    labels = path_scope.split(".")
    return tuple(".".join(labels[: i + 1]) for i in range(len(labels)))


def _guard(expression: str, clause_count: int, ceiling: int) -> None:
    if len(expression) > ceiling:
        raise CompilationRefused(len(expression), ceiling, clause_count)


def _descendant_denies(allow: str, deny_scopes: set[str]) -> list[str]:
    """The deny scopes that are STRICT descendants of ``allow`` (label-boundary
    aware via the trailing dot). Only these can defeat ``allow`` under
    longest-prefix-wins: a deny that is an ancestor of ``allow`` is shallower, so
    the deeper allow overrides it; a deny in an unrelated subtree can never be a
    co-ancestor of the same item (ancestors of a node form a chain)."""
    prefix = allow + "."
    return sorted(d for d in deny_scopes if d.startswith(prefix))


def compile_scope_filter(
    grants: list[PathGrant | Decision],
    *,
    action: str = "search_metadata",
    include_sidecars: bool = True,
    ceiling: int = FILTER_SIZE_CEILING,
) -> CompiledFilter:
    """Compile a principal's grants into a DENY-AWARE Meilisearch scope filter.

    Unlike the allow-only :func:`compile_filter`, this reproduces
    :func:`filearr.rbac.evaluate` EXACTLY for ``action`` (longest-prefix-wins,
    explicit-deny-wins at equal specificity) as a filter over the document
    ``path_scope`` ancestor array. Equivalence is property-tested against
    ``evaluate`` on random grant sets.

    Construction (proof sketch): for an item, the grant scopes that are its
    ancestors form a CHAIN (any two ancestors of one node are comparable), so a
    unique deepest matching grant exists and its effect is the decision. That is
    reproduced by ``OR`` over each allow scope ``a`` of the clause
    ``path_scope = a AND NOT path_scope IN [denies strictly under a]``: the item
    is allowed iff SOME allow ancestor has no deeper deny ancestor — i.e. the
    deepest ancestor grant is an allow. Same-scope allow+deny collapses to deny
    (deny-wins-at-tie) by subtracting deny scopes from the allow set first.

    Allow scopes with no descendant deny are grouped into one ``path_scope IN
    [...]`` term for compactness; when there are NO denies at all the whole
    expression collapses to ``path_scope IN [all allows]`` — byte-identical to
    :func:`compile_filter` for the pure-allow case.

    Raises :class:`CompilationRefused` (R2) when the expression exceeds
    ``ceiling`` — REFUSE, never coarsen (silent precision loss is forbidden)."""
    allow_scopes: set[str] = set()
    deny_scopes: set[str] = set()
    for g in grants:
        grant = g.grant if isinstance(g, Decision) else g
        if grant is None or grant.action != action:
            continue
        (allow_scopes if grant.allow else deny_scopes).add(grant.path)
    # Deny-wins at equal specificity: a scope that is BOTH allow and deny is a deny.
    allow_scopes -= deny_scopes

    simple: list[str] = []
    complex_terms: list[str] = []
    for a in sorted(allow_scopes):
        under = _descendant_denies(a, deny_scopes)
        if not under:
            simple.append(a)
        else:
            joined = ", ".join(_escape_scope(d) for d in under)
            complex_terms.append(
                f"({PATH_SCOPE_ATTR} = {_escape_scope(a)} AND "
                f"NOT {PATH_SCOPE_ATTR} IN [{joined}])"
            )

    terms: list[str] = []
    if simple:
        joined = ", ".join(_escape_scope(s) for s in simple)
        terms.append(f"{PATH_SCOPE_ATTR} IN [{joined}]")
    terms.extend(complex_terms)

    if not terms:
        # No effective allow scope → matches nothing (fail-closed).
        allow_expr = f"{PATH_SCOPE_ATTR} IN []"
    elif len(terms) == 1:
        allow_expr = terms[0]
    else:
        allow_expr = "(" + " OR ".join(terms) + ")"

    if include_sidecars:
        expression = allow_expr
    else:
        expression = f"({allow_expr}) AND {_ATTR_IS_SIDECAR} = false"

    clause_count = len(allow_scopes) + len(deny_scopes)
    _guard(expression, clause_count, ceiling)
    return CompiledFilter(expression=expression, scopes=tuple(sorted(allow_scopes)))


def rbac_filter_for(
    role: Role,
    grants: list[PathGrant | Decision],
    *,
    action: str = "search_metadata",
    include_sidecars: bool = True,
    ceiling: int = FILTER_SIZE_CEILING,
) -> str | None:
    """The request-time entry point: a principal's Meilisearch scope filter.

    Returns ``None`` for an UNRESTRICTED principal (``admin`` global role — its
    ceiling is "everything", mirroring ``evaluate``'s admin bypass; the caller
    also passes ``None`` when auth is disabled) so the caller injects no filter
    and the query is byte-identical to the pre-P6 shared-key path. Otherwise
    returns the deny-aware filter EXPRESSION to ``AND`` into every Meili query.

    A non-admin with zero grants compiles to ``path_scope IN []`` — sees nothing
    (fail-closed, matching ``evaluate``'s ``no_grant_default_deny``). Raises
    :class:`CompilationRefused` (→ the API maps it to 422 "consolidate grants")
    when the grant set is too complex to express under ``ceiling``: REFUSE, never
    silently widen or narrow."""
    if role is Role.ADMIN:
        return None
    return compile_scope_filter(
        grants,
        action=action,
        include_sidecars=include_sidecars,
        ceiling=ceiling,
    ).expression


def mint_tenant_token(
    compiled: CompiledFilter,
    *,
    parent_key: str,
    parent_key_uid: str,
    expires_in_seconds: int,
) -> str:
    """Sign a Meilisearch tenant token embedding ``compiled.expression`` (P6-T3).

    Stub — the real implementation signs with the principal's **per-user**
    Meilisearch parent key (R3) via ``meilisearch-python-sdk``, embeds the
    compiled search rule and a short expiry (brief §2.6 steps 3-5), and returns
    the JWT-like token string. Per-user parent keys are what make single-token
    revocation possible (rotate that one key) without invalidating every other
    principal's active token."""
    raise NotImplementedError(
        "mint_tenant_token is a Phase 6 P6-T3 stub — signs with a per-user "
        "Meilisearch parent key via meilisearch-python-sdk"
    )
