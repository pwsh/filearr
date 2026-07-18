"""Local accounts, sessions & the auth gate surface (Phase 6, P6-T1).

Endpoints (all under ``/api/v1/auth``):

* ``GET  /status``   — public probe: is auth enabled, do any users exist, what
  mode is the UI in (disabled / bootstrap / enabled). No auth required — the SPA
  calls this before deciding whether to show a login wall.
* ``POST /bootstrap`` — create the FIRST admin, allowed ONLY while zero users
  exist (409 afterwards). The first-run escape hatch so enabling auth never
  locks an operator out.
* ``POST /login``    — username/password → ``Set-Cookie: filearr_session``.
* ``POST /logout``   — revoke the current session + clear the cookie.
* ``GET  /me``       — the current session principal (401 if none).
* ``POST /password`` — self password change (verifies the current password).
* ``GET/POST /users`` + ``PATCH/DELETE /users/{id}`` — admin user management.

The login wall coexists with API keys: nothing here changes Bearer-key behaviour
and none of it engages while ``FILEARR_AUTH_ENABLED=false``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, authx, ratelimit
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Principal, User
from filearr.models import Session as SessionRow
from filearr.security import (
    clear_session_cookie,
    require_scope,
    resolve_session_principal,
    set_session_cookie,
)

router = APIRouter()

GlobalRole = Literal["admin", "user", "viewer"]


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class AuthStatus(BaseModel):
    auth_enabled: bool
    users_exist: bool
    mode: Literal["disabled", "bootstrap", "enabled"]
    # P6-T5: true when OIDC SSO is enabled AND minimally configured — the SPA
    # shows the "Sign in with SSO" button off this flag. Always false when auth is
    # disabled or OIDC is unconfigured (fail-closed).
    oidc_enabled: bool = False
    # P6-T6: true when LDAP is enabled AND minimally configured. The login form is
    # unchanged (same username/password POST); the SPA may show an optional
    # "directory sign-in supported" hint off this flag.
    ldap_enabled: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class BootstrapIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    global_role: GlobalRole = "user"
    email: str | None = None


class UserPatchIn(BaseModel):
    global_role: GlobalRole | None = None
    disabled: bool | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)


class PrincipalOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str | None
    global_role: str
    kind: str
    disabled: bool
    # P6-T12/T10: the identity source ('local'|'ldap'|'saml'|'oidc') so the admin
    # UI can badge federated accounts, and 'kind' distinguishes a human user from
    # a (future) service_account row.
    auth_provider: str = "local"


class LoginOut(BaseModel):
    principal: PrincipalOut
    # Surfaced when credentials were sent over plain http (temporarily allowed):
    # the frontend renders it as a nudge to switch to https://<host>:8443.
    warning: str | None = None


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _principal_out(principal: Principal, user: User) -> PrincipalOut:
    return PrincipalOut(
        id=principal.id,
        username=user.username,
        email=user.email,
        global_role=principal.global_role,
        kind=principal.kind,
        disabled=principal.disabled_at is not None,
        auth_provider=user.auth_provider,
    )


async def _users_exist(session: AsyncSession) -> bool:
    result = await session.execute(select(func.count()).select_from(User))
    return (result.scalar_one() or 0) > 0


async def _load_user(session: AsyncSession, principal_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.principal_id == principal_id))
    return result.scalar_one_or_none()


async def _ldap_eligible(session: AsyncSession, username: str) -> bool:
    """Whether ``/auth/login`` should fall through to LDAP for this username.

    Local-first ordering: an EXISTING local account (any provider other than
    ldap) blocks the fall-through — a same-named local admin stays local and a
    wrong local password never leaks to the directory. Only an unknown username
    or an already-ldap-sourced account is eligible."""
    normalized = username.strip().lower()
    result = await session.execute(select(User).where(User.username == normalized))
    user = result.scalar_one_or_none()
    return user is None or user.auth_provider == "ldap"


async def current_principal(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Dependency: the authenticated session principal, or 401. Session-only
    (Bearer keys are not yet mapped to principals — that is the ApiKey backfill,
    a later additive pass)."""
    principal = await resolve_session_principal(request, response, session)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return principal


def _https_warning(request: Request) -> str | None:
    from filearr.security import _request_is_https

    if _request_is_https(request):
        return None
    return (
        "Credentials were sent over plain http. Use the TLS endpoint "
        "(https://<host>:8443) so the session cookie is protected in transit."
    )


# --------------------------------------------------------------------------- #
# Public probe                                                                 #
# --------------------------------------------------------------------------- #
@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(session: AsyncSession = Depends(get_session)) -> AuthStatus:
    settings = get_settings()
    exists = await _users_exist(session)
    if not settings.auth_enabled:
        mode: Literal["disabled", "bootstrap", "enabled"] = "disabled"
    elif not exists:
        mode = "bootstrap"
    else:
        mode = "enabled"
    return AuthStatus(
        auth_enabled=settings.auth_enabled,
        users_exist=exists,
        mode=mode,
        oidc_enabled=settings.oidc_is_configured,
        ldap_enabled=settings.ldap_is_configured,
    )


# --------------------------------------------------------------------------- #
# Bootstrap / login / logout / me                                             #
# --------------------------------------------------------------------------- #
@router.post("/auth/bootstrap", response_model=PrincipalOut, status_code=201)
async def bootstrap(
    payload: BootstrapIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    """Create the first admin. Allowed only while zero users exist (409 after).
    Deliberately unauthenticated — it is the first-run escape hatch."""
    # P6-T8: the same brute-force lock gate guards bootstrap (a locked IP cannot
    # hammer it either).
    retry_after = await ratelimit.check_locked(payload.username, ratelimit.client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if await _users_exist(session):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user already exists; bootstrap is closed")
    principal = Principal(kind="user", global_role="admin")
    session.add(principal)
    await session.flush()
    user = User(
        principal_id=principal.id,
        username=payload.username.strip().lower(),
        password_hash=authx.hash_password(payload.password),
        auth_provider="local",
    )
    session.add(user)
    await session.commit()
    await audit.emit(
        audit.BOOTSTRAP,
        request=request,
        principal_id=principal.id,
        username_attempted=payload.username,
    )
    return _principal_out(principal, user)


@router.post("/auth/login", response_model=LoginOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> LoginOut:
    settings = get_settings()
    ip = ratelimit.client_ip(request)
    # P6-T8: reject a locked username/IP BEFORE the slow argon2 verify runs. The
    # 429 is byte-identical for an unknown vs a known username (anti-enumeration).
    retry_after = await ratelimit.check_locked(payload.username, ip)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    principal = await authx.authenticate_local(session, payload.username, payload.password)
    ldap_result = None
    provider = "local"
    # Local-first, then LDAP fall-through (P6-T6): only when local auth did not
    # succeed AND the username is unknown or ldap-sourced. Same login form; the
    # directory verifies the password via a real bind.
    if principal is None:
        if settings.ldap_is_configured and await _ldap_eligible(session, payload.username):
            from filearr import ldap_auth

            try:
                ldap_result = await ldap_auth.authenticate_ldap(
                    session, payload.username, payload.password
                )
            except ldap_auth.LDAPError:
                # Config/transport/role-refusal → generic failure (fail-closed);
                # discard any partial provisioning writes.
                await session.rollback()
                ldap_result = None
            if ldap_result is not None:
                principal = await session.get(Principal, uuid.UUID(ldap_result.principal_id))
                provider = "ldap"
    if principal is None:
        await session.rollback()
        # Counter mutations + audit run in their OWN transactions, so the rollback
        # above never discards them (P6-T8/T9).
        newly_locked = await ratelimit.register_failure(payload.username, ip)
        await audit.emit(
            audit.LOGIN_FAILURE, request=request, username_attempted=payload.username
        )
        if newly_locked:
            await audit.emit(
                audit.LOCKOUT, request=request, username_attempted=payload.username
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = await authx.create_session(
        session,
        str(principal.id),
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    # A successful auth clears the username bucket (the IP bucket decays naturally).
    await ratelimit.clear_username(payload.username)
    # An LDAP login may have changed the mapped role or synced groups → the
    # effective grants moved; drop the grant cache (P6-T4).
    if ldap_result is not None and (ldap_result.role_changed or ldap_result.groups_changed):
        from filearr import grant_cache

        grant_cache.bump_generation()
    set_session_cookie(response, request, token.raw)
    user = await _load_user(session, principal.id)
    assert user is not None
    await audit.emit(
        audit.LDAP_LOGIN if provider == "ldap" else audit.LOGIN_SUCCESS,
        request=request,
        principal_id=principal.id,
        username_attempted=payload.username,
    )
    return LoginOut(principal=_principal_out(principal, user), warning=_https_warning(request))


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Response:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if raw:
        # Resolve the owning principal for the audit WITHOUT rotating the token
        # (a plain hash lookup — revoke_session would otherwise miss a rotated row).
        row = (
            await session.execute(
                select(SessionRow).where(
                    SessionRow.session_hash == authx.hash_session_token(raw)
                )
            )
        ).scalar_one_or_none()
        pid = row.principal_id if row is not None else None
        await authx.revoke_session(session, raw)
        await session.commit()
        if pid is not None:
            await audit.emit(audit.LOGOUT, request=request, principal_id=pid)
    clear_session_cookie(response, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=PrincipalOut)
async def me(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    user = await _load_user(session, principal.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return _principal_out(principal, user)


@router.post("/auth/password", status_code=204)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Self-service password change. Verifies the current password, then kills
    every session for the principal (a password change is a privilege event).
    Rate-limited like login so a wrong-current-password guessing loop locks."""
    user = await _load_user(session, principal.id)
    username = user.username if user is not None else None
    ip = ratelimit.client_ip(request)
    retry_after = await ratelimit.check_locked(username, ip)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if user is None or not authx.verify_password(payload.current_password, user.password_hash):
        newly_locked = await ratelimit.register_failure(username, ip)
        await audit.emit(
            audit.LOGIN_FAILURE,
            request=request,
            principal_id=principal.id,
            username_attempted=username,
        )
        if newly_locked:
            await audit.emit(
                audit.LOCKOUT,
                request=request,
                principal_id=principal.id,
                username_attempted=username,
            )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    user.password_hash = authx.hash_password(payload.new_password)
    await authx.revoke_all_for_principal(session, str(principal.id))
    await session.commit()
    await ratelimit.clear_username(username)
    await audit.emit(
        audit.PASSWORD_CHANGE,
        request=request,
        principal_id=principal.id,
        username_attempted=username,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Admin user management                                                        #
# --------------------------------------------------------------------------- #
@router.get(
    "/auth/users",
    response_model=list[PrincipalOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_users(session: AsyncSession = Depends(get_session)) -> list[PrincipalOut]:
    result = await session.execute(
        select(Principal, User)
        .join(User, User.principal_id == Principal.id)
        .order_by(User.username)
    )
    return [_principal_out(p, u) for p, u in result.all()]


@router.post(
    "/auth/users",
    response_model=PrincipalOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_user(
    payload: UserCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    normalized = payload.username.strip().lower()
    existing = await session.execute(select(User).where(User.username == normalized))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"User '{normalized}' already exists")
    principal = Principal(kind="user", global_role=payload.global_role)
    session.add(principal)
    await session.flush()
    user = User(
        principal_id=principal.id,
        username=normalized,
        email=payload.email,
        password_hash=authx.hash_password(payload.password),
        auth_provider="local",
    )
    session.add(user)
    await session.commit()
    await audit.emit(
        audit.USER_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal.id), "username": normalized, "role": payload.global_role},
    )
    return _principal_out(principal, user)


@router.patch(
    "/auth/users/{principal_id}",
    response_model=PrincipalOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def patch_user(
    principal_id: uuid.UUID,
    payload: UserPatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PrincipalOut:
    principal = await session.get(Principal, principal_id)
    user = await _load_user(session, principal_id)
    if principal is None or user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    privilege_change = False
    role_changed_to: str | None = None
    disabled_changed_to: bool | None = None
    if payload.global_role is not None and payload.global_role != principal.global_role:
        principal.global_role = payload.global_role
        role_changed_to = payload.global_role
        privilege_change = True
    if payload.disabled is not None:
        from datetime import UTC, datetime

        new_disabled_at = datetime.now(UTC) if payload.disabled else None
        if (new_disabled_at is not None) != (principal.disabled_at is not None):
            principal.disabled_at = new_disabled_at
            disabled_changed_to = payload.disabled
            privilege_change = True
    if payload.email is not None:
        user.email = payload.email
    if payload.password is not None:
        user.password_hash = authx.hash_password(payload.password)
        privilege_change = True
    # A change of authority (role/disable/password) revokes existing sessions so
    # it takes effect immediately (instant-revocation, research §1.3).
    if privilege_change:
        await authx.revoke_all_for_principal(session, str(principal_id))
    await session.commit()
    if privilege_change:
        # A role change alters the principal's effective grants/ceiling — drop the
        # cached grant set so no stale decision survives (P6-T4).
        from filearr import grant_cache

        grant_cache.bump_generation()
    actor = audit.actor_id(request)
    if role_changed_to is not None:
        await audit.emit(
            audit.ROLE_CHANGED,
            request=request,
            principal_id=actor,
            details={"target": str(principal_id), "role": role_changed_to},
        )
    if disabled_changed_to is not None:
        await audit.emit(
            audit.USER_DISABLED if disabled_changed_to else audit.USER_ENABLED,
            request=request,
            principal_id=actor,
            details={"target": str(principal_id)},
        )
    return _principal_out(principal, user)


@router.delete(
    "/auth/users/{principal_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_user(
    principal_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    principal = await session.get(Principal, principal_id)
    if principal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # Refuse to delete the last remaining admin so auth can never be locked out.
    if principal.global_role == "admin":
        remaining = await session.execute(
            select(func.count())
            .select_from(Principal)
            .where(Principal.global_role == "admin", Principal.id != principal_id)
        )
        if (remaining.scalar_one() or 0) == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Cannot delete the last admin account"
            )
    await session.delete(principal)  # CASCADE removes the user row + sessions
    await session.commit()
    from filearr import grant_cache

    grant_cache.bump_generation()  # principal's grants/memberships gone (P6-T4)
    await audit.emit(
        audit.USER_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal_id)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Session management (P6-T11) — "active sessions" + remote logout             #
# --------------------------------------------------------------------------- #
class SessionOut(BaseModel):
    id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    # True for the caller's OWN current session (own-list only) so the UI can
    # label it and refuse a footgun self-revoke without warning.
    current: bool = False


def _session_out(row: SessionRow, current_id: str | None) -> SessionOut:
    return SessionOut(
        id=row.id,
        ip_address=str(row.ip_address) if row.ip_address is not None else None,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        current=current_id is not None and str(row.id) == current_id,
    )


async def _sessions_for(session: AsyncSession, principal_id: uuid.UUID) -> list[SessionRow]:
    result = await session.execute(
        select(SessionRow)
        .where(SessionRow.principal_id == principal_id)
        .order_by(SessionRow.last_seen_at.desc())
    )
    return list(result.scalars().all())


@router.get("/auth/sessions", response_model=list[SessionOut])
async def list_my_sessions(
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    """The caller's own active sessions (IP / user-agent / last-seen), with the
    current one flagged."""
    current_id = getattr(request.state, "session_id", None)
    rows = await _sessions_for(session, principal.id)
    return [_session_out(r, current_id) for r in rows]


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_my_session(
    session_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke one of the caller's OWN sessions (remote logout). 404 if the id is
    unknown or belongs to another principal (never leak another user's session).
    The revoked session dies on its very next request (instant revocation)."""
    row = await session.get(SessionRow, session_id)
    if row is None or row.principal_id != principal.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await session.delete(row)
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=principal.id,
        details={"session_id": str(session_id), "self": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/auth/sessions/revoke-all", status_code=204)
async def revoke_all_my_sessions(
    request: Request,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """"Log out everywhere" — kill every one of the caller's sessions (including
    this one). Other principals' sessions are untouched."""
    n = await authx.revoke_all_for_principal(session, str(principal.id))
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=principal.id,
        details={"scope": "all", "count": n, "self": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/auth/users/{principal_id}/sessions",
    response_model=list[SessionOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_user_sessions(
    principal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    """Admin: list any principal's active sessions."""
    rows = await _sessions_for(session, principal_id)
    return [_session_out(r, None) for r in rows]


@router.delete(
    "/auth/users/{principal_id}/sessions",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def revoke_user_sessions(
    principal_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Admin: force-log-out a principal everywhere (kills only that principal's
    sessions)."""
    n = await authx.revoke_all_for_principal(session, str(principal_id))
    await session.commit()
    await audit.emit(
        audit.SESSION_REVOKED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"target": str(principal_id), "scope": "all", "count": n},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
