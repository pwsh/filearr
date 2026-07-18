"""Request-time RBAC → Meilisearch scope-filter resolution (Phase 6, P6-T3).

The **server-side proxy** enforcement path: given the authenticated principal,
load their effective grants from Postgres and compile the deny-aware
Meilisearch scope filter that every search-surface query is narrowed by. Meili
does the row-level filtering — the API never post-filters hits.

Kept out of ``security.py`` (which owns the auth carriers) and out of
``api/rbac.py`` (which imports ``security``) so the dependency in ``security``
can import this without a cycle. Pure filter-compilation lives in
``filearr.tenant_tokens``; this module is only the DB read + config glue.

Staleness (documented, enforced cheaply): the filter is recomputed from live DB
grants on EVERY request — there is no grant cache in P6-T3, so a grant/role edit
takes effect on the caller's very next search (bounded only by session-token
freshness; a role change also revokes sessions via
``authx.revoke_all_for_principal``). P6-T4's grant-cache design MUST carry its
own invalidation; until then, correctness-over-latency is deliberate."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import rbac, tenant_tokens
from filearr.config import get_settings
from filearr.models import PathGrant, Principal, PrincipalGroupMember


async def load_principal_grants(
    session: AsyncSession, principal_id: uuid.UUID
) -> tuple[rbac.Role, list[rbac.PathGrant]]:
    """Load a principal's global role + effective grants (its own direct grants
    plus those of every group it belongs to) as pure ``rbac.PathGrant`` objects —
    the exact input ``rbac.evaluate`` / ``tenant_tokens.compile_scope_filter``
    consume. An unknown role maps to VIEWER (fail-closed)."""
    principal = await session.get(Principal, principal_id)
    if principal is None:
        return rbac.Role.VIEWER, []
    try:
        role = rbac.Role(principal.global_role)
    except ValueError:
        role = rbac.Role.VIEWER
    group_ids = (
        (
            await session.execute(
                select(PrincipalGroupMember.group_id).where(
                    PrincipalGroupMember.principal_id == principal_id
                )
            )
        )
        .scalars()
        .all()
    )
    conds = [
        and_(PathGrant.subject_kind == "principal", PathGrant.subject_id == principal_id)
    ]
    if group_ids:
        conds.append(
            and_(PathGrant.subject_kind == "group", PathGrant.subject_id.in_(group_ids))
        )
    rows = (await session.execute(select(PathGrant).where(or_(*conds)))).scalars().all()
    grants = [
        rbac.PathGrant(
            path=r.scope,
            action=r.action,
            allow=(r.effect == "allow"),
            group_ref=str(r.subject_id) if r.subject_kind == "group" else None,
            principal_ref=str(r.subject_id) if r.subject_kind == "principal" else None,
        )
        for r in rows
    ]
    return role, grants


async def scope_filter_for_principal(
    session: AsyncSession,
    principal: Principal,
    *,
    action: str = "search_metadata",
    include_sidecars: bool = True,
    request=None,
) -> str | None:
    """Resolve the Meilisearch scope-filter expression for ``principal``.

    ``None`` = unrestricted (admin global role) — inject no filter. Otherwise the
    deny-aware filter to ``AND`` into the Meili query. Raises
    ``tenant_tokens.CompilationRefused`` when the grant set exceeds the (config)
    size ceiling — the caller maps it to HTTP 422 (consolidate grants).

    When ``request`` is supplied the principal's grants are resolved through the
    P6-T4 two-tier grant cache (per-request memo + short-TTL process cache), so a
    request that also runs a SQL ``WHERE`` / per-item check shares ONE grant
    fetch. Without a request (e.g. the P6-T3 unit tests) it reads the DB directly
    — byte-identical result."""
    if request is not None:
        from filearr import grant_cache

        role, grants = await grant_cache.load_grants(request, session, principal.id)
    else:
        role, grants = await load_principal_grants(session, principal.id)
    return tenant_tokens.rbac_filter_for(
        role,
        grants,
        action=action,
        include_sidecars=include_sidecars,
        ceiling=get_settings().meili_scope_filter_ceiling,
    )
