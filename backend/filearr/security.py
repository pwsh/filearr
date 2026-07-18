"""Auth gate. Two coexisting credential carriers (research §2.2):

* **Bearer API keys** — prefixed CSPRNG, sha256-hashed at rest (high entropy, so
  no slow KDF needed). Scopes: read / write / admin. Used by *arr-style
  integrations, scripts, other agents. Unchanged from v1.
* **Interactive sessions** (P6-T1) — a Postgres-backed HttpOnly cookie
  (``filearr_session``). A logged-in user's ``global_role`` maps onto the same
  read/write/admin scope vocabulary (``authx.scopes_for_role``) so the existing
  ``require_scope`` dependencies accept either carrier with no per-route change.

Auth can be disabled wholesale for a trusted LAN (``FILEARR_AUTH_ENABLED=false``)
— in that mode this module is a no-op and behaves byte-for-byte as it did before
Phase 6 (no login wall, no cookie handling, existing key scopes untouched)."""

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import authx
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import ApiKey, Item, Principal

_bearer = HTTPBearer(auto_error=False)

KEY_PREFIX = "ck"


def generate_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, sha256_hash). Show full_key to the user once."""
    body = secrets.token_urlsafe(32)
    prefix = f"{KEY_PREFIX}_{body[:6]}"
    full = f"{prefix}_{body[6:]}"
    return full, prefix, hashlib.sha256(full.encode()).hexdigest()


async def _verify_credentials(
    token: str,
    scope: str,
    session: AsyncSession,
    request: Request | None = None,
) -> ApiKey:
    """Validate a raw bearer token against `scope` and record usage.

    Shared by `require_scope` (header auth) and the SSE events endpoint (which
    also accepts the token via query param because `EventSource` can't set
    headers). The token is hashed for the lookup; it is never logged or echoed.
    Raises 401/403 on failure. Returns the matched `ApiKey` on success.
    """
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if api_key is None or (api_key.expires_at and api_key.expires_at < now):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired API key")
    if scope not in api_key.scopes and "admin" not in api_key.scopes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Scope '{scope}' required")
    api_key.last_used_at = now
    if request is not None:
        request.state.actor = api_key.prefix
    await session.commit()
    return api_key


def _request_is_https(request: Request) -> bool:
    """True if the request reached us over TLS, honouring ``X-Forwarded-Proto``
    from the Caddy TLS front (OPS-T1). Gates the cookie ``Secure`` flag: set only
    over https so plain-http LAN access (temporarily allowed) still works."""
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    """Set the ``filearr_session`` cookie: HttpOnly always, SameSite from
    ``FILEARR_SESSION_COOKIE_SAMESITE`` (default ``lax`` — the P6-T5 ruling that
    lets an OIDC callback's cross-site 302→/ return carry the freshly-minted
    cookie; ``lax`` still blocks the cookie on cross-site POST/PATCH/DELETE so
    CSRF protection on every mutating endpoint is preserved), and Secure whenever
    the request is https. Max-Age tracks the absolute TTL."""
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite=settings.session_cookie_samesite,
        secure=_request_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite=settings.session_cookie_samesite,
        secure=_request_is_https(request),
    )


async def resolve_session_principal(
    request: Request,
    response: Response,
    session: AsyncSession,
) -> Principal | None:
    """Resolve the session-cookie principal for this request, or ``None``.

    Validates the cookie token (bumping the inactivity window and rotating the
    opaque token when due — re-setting the cookie on ``response``), then loads the
    owning principal. A disabled principal resolves to ``None`` (fails closed)."""
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    validated = await authx.validate_session(session, raw)
    if validated is None:
        return None
    principal = await session.get(Principal, validated.principal_id)
    if principal is None or principal.disabled_at is not None:
        return None
    # Stash the session row id (stable across token rotation) so the session-
    # management UI can flag the caller's CURRENT session (P6-T11).
    request.state.session_id = validated.session_id
    if validated.rotated is not None:
        set_session_cookie(response, request, validated.rotated.raw)
    return principal


def require_scope(scope: str):
    async def dependency(
        request: Request,
        response: Response,
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> None:
        settings = get_settings()
        if not settings.auth_enabled:
            return
        # 1. Bearer API key (unchanged v1 path — takes precedence when present).
        if creds is not None:
            await _verify_credentials(creds.credentials, scope, session, request)
            return
        # 2. Interactive session cookie (P6-T1). A logged-in user's global role
        #    maps onto the read/write/admin scope vocabulary.
        principal = await resolve_session_principal(request, response, session)
        if principal is not None:
            granted = authx.scopes_for_role(principal.global_role)
            if scope not in granted:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"Scope '{scope}' required")
            request.state.actor = f"principal:{principal.id}"
            await session.commit()
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    return dependency


# --------------------------------------------------------------------------- #
# P6-T3 — search-scope dependency (server-side proxy RBAC filter).            #
# --------------------------------------------------------------------------- #
def require_search_scope(scope: str = "read"):
    """Auth gate for a SEARCH-surface endpoint that ALSO returns the caller's
    Meilisearch scope filter (P6-T3). Drop-in replacement for
    ``Depends(require_scope(scope))`` on Meili-querying endpoints — it performs
    the identical authentication, then hands back the deny-aware scope-filter
    EXPRESSION (or ``None``) for the endpoint to ``AND`` into its Meili query.

    Return contract (the value injected as the endpoint parameter):

    * ``None`` — inject NO filter (query byte-identical to the pre-P6 path):
      auth disabled, a Bearer API key (trusted integration; per-key path scoping
      is future service-account work — the shared-key search path stays
      untouched), or an ``admin`` session principal (unrestricted, mirrors
      ``rbac.evaluate``'s admin bypass).
    * a filter string — a non-admin session principal's compiled scope filter;
      a principal with no grants compiles to ``path_scope IN []`` (sees nothing,
      fail-closed).

    A single principal resolution happens here (no double session-token rotation
    vs a separate ``require_scope``). An over-ceiling grant set raises
    ``CompilationRefused`` → HTTP 422 (consolidate grants) rather than coarsening
    (R2). The filter is recomputed from live DB grants every request — no cache
    (P6-T4 owns the grant cache + its invalidation)."""
    from filearr import authx
    from filearr.search_scope import scope_filter_for_principal
    from filearr.tenant_tokens import CompilationRefused

    async def dependency(
        request: Request,
        response: Response,
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> str | None:
        settings = get_settings()
        if not settings.auth_enabled:
            return None
        # 1. Bearer API key — unchanged v1 path; unrestricted search scope.
        if creds is not None:
            await _verify_credentials(creds.credentials, scope, session, request)
            return None
        # 2. Interactive session cookie (single resolution; rotates if due).
        principal = await resolve_session_principal(request, response, session)
        if principal is not None:
            granted = authx.scopes_for_role(principal.global_role)
            if scope not in granted:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"Scope '{scope}' required")
            request.state.actor = f"principal:{principal.id}"
            try:
                scope_filter = await scope_filter_for_principal(
                    session, principal, request=request
                )
            except CompilationRefused as exc:
                # R2: refuse, never coarsen — the admin must consolidate grants.
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
                ) from exc
            await session.commit()
            return scope_filter
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    return dependency


# --------------------------------------------------------------------------- #
# P6-T4 — require_permission: route-level path-scoped RBAC enforcement.        #
#                                                                              #
# Layered ON TOP of the coarse read/write/admin scopes: `require_scope` stays  #
# the coarse gate, `require_permission` refines it per RESOURCE using the pure #
# `rbac.evaluate` engine over the item's `path_scope`. On a migrated endpoint  #
# it REPLACES the coarse `Depends(require_scope(...))` (it performs the         #
# identical authentication + coarse-scope check itself, then returns a         #
# `PermissionContext`) so a single session resolution / token rotation happens #
# per request — the same pattern P6-T3's `require_search_scope` established.    #
#                                                                              #
# Fast path (byte-identical legacy behaviour): auth disabled, a Bearer API key #
# (trusted integration — per-key path scoping is future service-account work), #
# or an `admin` session principal → an UNRESTRICTED context (no per-item check,#
# no SQL filter). Existing `require_scope` tests are unaffected.               #
# --------------------------------------------------------------------------- #

#: RBAC action → coarse scope it lives under (the pre-existing read/write/admin
#: gate). Search/read/download-tier actions need `read`; mutation-tier actions
#: need `write`. A caller may override (e.g. transfers keep `write` for the
#: download action — bytes leaving a machine is mutation-tier there).
_ACTION_COARSE_SCOPE: dict[str, str] = {
    "search_metadata": "read",
    "search_content": "read",
    "download": "read",
    "upload": "write",
    "modify": "write",
    "delete": "write",
    "edit_metadata": "write",
    "manage_alerts": "write",
}

#: The base "can see this at all" action — an item readable in search is readable
#: here. Used for the 404-vs-403 split (below).
VISIBILITY_ACTION = "search_metadata"


class PermissionContext:
    """The resolved RBAC decision surface for one request, returned by
    :func:`require_permission`. Carries the principal's role + effective grants
    (resolved ONCE via the P6-T4 grant cache) so both a collection ``WHERE``
    clause and per-item checks reuse a single fetch.

    ``unrestricted`` is the admin / API-key / auth-off fast path: every check is
    an immediate allow and every SQL clause is ``None`` (no filter) — legacy
    behaviour, byte-for-byte."""

    __slots__ = ("unrestricted", "role", "grants", "action", "use_ltree", "principal")

    def __init__(
        self,
        *,
        unrestricted: bool,
        action: str,
        role=None,
        grants=None,
        use_ltree: bool = False,
        principal: Principal | None = None,
    ) -> None:
        self.unrestricted = unrestricted
        self.action = action
        self.role = role
        self.grants = grants or []
        self.use_ltree = use_ltree
        self.principal = principal

    def _allows(self, path_scope: str | None, action: str) -> bool:
        from filearr import rbac

        if self.unrestricted:
            return True
        if path_scope is None:
            return False  # unstamped item → invisible to a scoped principal
        return rbac.evaluate(self.grants, self.role, path_scope, action).allowed

    def authorize_item(self, item, *, action: str | None = None) -> None:
        """Enforce the 404-vs-403 ruling for ``item``:

        * the item is UNREADABLE (no visibility permission) → **404** (never leak
          the existence of an item outside the caller's scope); and
        * the item is readable but the ACTION (write-tier) is denied → **403**.

        The read-only default action is the visibility action, so a plain GET of
        an unreadable item is a 404 and no separate 403 arises."""
        if self.unrestricted:
            return
        act = action or self.action
        path_scope = getattr(item, "path_scope", None)
        if act == VISIBILITY_ACTION:
            # A plain read: a denial (or no grant) is a 404 — never leak existence.
            if not self._allows(path_scope, VISIBILITY_ACTION):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
            return
        # A write/download-tier action. If it is granted, allow (a grant on the
        # action implies the item is within scope — no separate read grant
        # needed). Otherwise split 404 (not even visible) vs 403 (visible, action
        # denied) so an out-of-scope item's existence still never leaks.
        if self._allows(path_scope, act):
            return
        if self._allows(path_scope, VISIBILITY_ACTION):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Action '{act}' denied for this item"
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    def can(self, item, *, action: str | None = None) -> bool:
        """Non-raising variant of :meth:`authorize_item` (bool)."""
        if self.unrestricted:
            return True
        act = action or self.action
        return self._allows(getattr(item, "path_scope", None), act)

    def require_capability(self, action: str) -> None:
        """Assert a scoped principal is CAPABLE of ``action`` at all (T10 gate).

        Used by the report/export DOWNLOAD paths: a principal who can VIEW a
        report on screen (``search_metadata``) but was never granted ``download``
        must be refused a file export/artifact. Unrestricted (admin / API key /
        auth-off) → no-op (legacy behaviour, byte-for-byte). Otherwise a 403 when
        the action exceeds the role ceiling OR the principal holds no allow grant
        for it (the per-row ``sql_clause(action=...)`` still scopes which rows the
        capable principal actually receives)."""
        from filearr import rbac

        if self.unrestricted:
            return
        if action not in rbac.ROLE_CEILINGS.get(self.role, frozenset()):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Action '{action}' is not permitted for your role",
            )
        if not any(g.action == action and g.allow for g in self.grants):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Action '{action}' requires a path grant",
            )

    def scope_snapshot(self) -> dict | None:
        """Serialize this context's scope for a BACKGROUND export job (P11-T5).

        A queued export runs without the originating request, so the caller's
        resolved grants are snapshotted at enqueue time and rebuilt in the job to
        scope its rows (``filearr.exports.scope_clause_from_snapshot``). Returns
        ``None`` for an unrestricted context (no filter). This is a static grant
        snapshot; the download endpoint independently re-checks capability +
        ownership at fetch time (T10)."""
        if self.unrestricted:
            return None
        role = self.role
        return {
            "role": role.value if hasattr(role, "value") else str(role),
            "use_ltree": self.use_ltree,
            "grants": [
                {"path": g.path, "action": g.action, "allow": g.allow}
                for g in self.grants
            ],
        }

    def sql_clause(self, column=None, *, action: str | None = None):
        """A SQLAlchemy ``WHERE`` predicate scoping a collection query to the
        caller's readable rows, or ``None`` (unrestricted → inject no filter).
        Defaults to the item ``path_scope`` column and the visibility action —
        a listing shows exactly what the caller could see in search."""
        from filearr import rbac_sql

        if self.unrestricted:
            return None
        col = column if column is not None else Item.path_scope
        return rbac_sql.scope_where_clause(
            self.role,
            self.grants,
            action=action or VISIBILITY_ACTION,
            column=col,
            use_ltree=self.use_ltree,
        )

    def search_filter(self, *, action: str | None = None, include_sidecars: bool = True):
        """The caller's deny-aware Meilisearch scope filter (or ``None`` when
        unrestricted), for endpoints that query Meili off the SAME resolved
        grants (e.g. ``/items/{id}/similar``). Raises
        ``tenant_tokens.CompilationRefused`` above the size ceiling — the caller
        maps it to HTTP 422 (consolidate grants), exactly like
        ``require_search_scope``."""
        from filearr import tenant_tokens

        if self.unrestricted:
            return None
        return tenant_tokens.rbac_filter_for(
            self.role,
            self.grants,
            action=action or VISIBILITY_ACTION,
            include_sidecars=include_sidecars,
            ceiling=get_settings().meili_scope_filter_ceiling,
        )


def require_permission(action: str, *, coarse: str | None = None):
    """Dependency factory: authenticate + coarse-scope-gate for ``action`` and
    return a :class:`PermissionContext` for per-resource RBAC.

    ``coarse`` overrides the default action→scope mapping (e.g. transfers keep
    ``write`` for the ``download`` action). Mirrors ``require_scope`` /
    ``require_search_scope`` exactly on the auth carriers; the only addition is
    resolving the scoped principal's grants (once, cached) for the returned
    context."""
    from filearr import authx, grant_cache, rbac, rbac_sql

    coarse_scope = coarse or _ACTION_COARSE_SCOPE.get(action, "read")

    async def dependency(
        request: Request,
        response: Response,
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
        session: AsyncSession = Depends(get_session),
    ) -> PermissionContext:
        settings = get_settings()
        if not settings.auth_enabled:
            return PermissionContext(unrestricted=True, action=action)
        # 1. Bearer API key — trusted integration, unrestricted (legacy path).
        if creds is not None:
            await _verify_credentials(creds.credentials, coarse_scope, session, request)
            return PermissionContext(unrestricted=True, action=action)
        # 2. Interactive session cookie (single resolution; rotates if due).
        principal = await resolve_session_principal(request, response, session)
        if principal is not None:
            granted = authx.scopes_for_role(principal.global_role)
            if coarse_scope not in granted:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, f"Scope '{coarse_scope}' required"
                )
            request.state.actor = f"principal:{principal.id}"
            if principal.global_role == rbac.Role.ADMIN.value:
                await session.commit()
                return PermissionContext(
                    unrestricted=True, action=action, principal=principal
                )
            role, grants = await grant_cache.load_grants(request, session, principal.id)
            use_ltree = await rbac_sql.path_scope_uses_ltree(session)
            await session.commit()
            return PermissionContext(
                unrestricted=False,
                action=action,
                role=role,
                grants=grants,
                use_ltree=use_ltree,
                principal=principal,
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    return dependency
