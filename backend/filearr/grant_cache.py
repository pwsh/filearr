"""Request- and process-scoped RBAC grant cache (Phase 6, P6-T4).

Every RBAC-enforced request may consult a principal's grants three times — the
search-scope filter, a collection ``WHERE`` clause, and per-item checks. Fetching
them from Postgres each time is wasteful, and P6-T3 explicitly deferred the cache
(and its invalidation) to this task.

Two tiers, both fail-safe toward freshness:

1. **Per-request memoization** — the first resolution in a request stores
   ``(role, grants)`` on ``request.state`` keyed by principal id; every later
   consumer in the SAME request reuses it. Guarantees "one grant fetch per
   request" (the acceptance metric) and a self-consistent decision across the
   filter + WHERE + item checks within one request.

2. **Short-TTL process cache with generation invalidation** — resolutions are
   also cached process-wide for ``_TTL_SECONDS`` (30s). A single module
   **generation counter** is bumped by :func:`bump_generation` on ANY grant /
   group / membership mutation (wired into the RBAC admin handlers), which
   instantly invalidates every cached entry — so a revoked grant never lingers
   behind the TTL. The TTL is only a backstop against unbounded staleness for
   changes that (in principle) bypass the counter; in practice invalidation is
   immediate. Role changes / disables additionally revoke the principal's
   sessions, so a stale role can't be exercised at all.

Security note: caching an ACL is a staleness risk, so the invalidation is
generation-based (not time-based) — correctness-first per the project ordering.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from filearr import rbac
from filearr.models import Principal
from filearr.search_scope import load_principal_grants

_TTL_SECONDS = 30.0

# Bumped on every grant/group/membership mutation → invalidates the process cache.
_generation = 0
# principal_id -> (generation, expires_monotonic, role, grants)
_cache: dict[uuid.UUID, tuple[int, float, rbac.Role, list[rbac.PathGrant]]] = {}


def bump_generation() -> None:
    """Invalidate every cached grant set. Called by the RBAC admin handlers after
    a grant/group/membership mutation (and role change / disable) so a scoped
    principal's very next request recompiles from live DB state."""
    global _generation
    _generation += 1


def _memo(request) -> dict:
    memo = getattr(request.state, "_rbac_grant_memo", None)
    if memo is None:
        memo = {}
        request.state._rbac_grant_memo = memo
    return memo


async def load_grants(
    request, session: AsyncSession, principal_id: uuid.UUID
) -> tuple[rbac.Role, list[rbac.PathGrant]]:
    """Resolve ``(role, grants)`` for ``principal_id`` via the two-tier cache.

    ``request`` may be ``None`` (non-HTTP call sites) — then only the process
    cache applies. A DB fetch happens at most once per request and at most once
    per TTL window / generation per process."""
    if request is not None:
        memo = _memo(request)
        hit = memo.get(principal_id)
        if hit is not None:
            return hit

    now = time.monotonic()
    ent = _cache.get(principal_id)
    if ent is not None and ent[0] == _generation and ent[1] > now:
        role, grants = ent[2], ent[3]
    else:
        role, grants = await load_principal_grants(session, principal_id)
        _cache[principal_id] = (_generation, now + _TTL_SECONDS, role, grants)

    if request is not None:
        _memo(request)[principal_id] = (role, grants)
    return role, grants


async def load_grants_for(
    request, session: AsyncSession, principal: Principal
) -> tuple[rbac.Role, list[rbac.PathGrant]]:
    """Convenience wrapper keyed off a resolved :class:`Principal`."""
    return await load_grants(request, session, principal.id)
