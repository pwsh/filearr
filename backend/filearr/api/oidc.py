"""OIDC SSO endpoints (Phase 6, P6-T5).

Two routes under ``/api/v1/auth/oidc``:

* ``GET /login``    — start the flow: generate PKCE + ``state`` + ``nonce`` (stored
  server-side, single-use, TTL), 302 to the IdP authorization endpoint.
* ``GET /callback`` — the IdP return: validate ``state``, exchange the code (PKCE),
  validate the ID token (sig/iss/aud/exp/nonce/at_hash), JIT-provision or link the
  principal, MINT THE STANDARD P6-T1 SESSION COOKIE, 302 to the app.

Both fail closed: when OIDC is not enabled+configured they 404 (so an auth-off /
un-configured deployment is byte-for-byte unchanged), and any error in the flow
redirects to the SPA root with a generic ``?sso_error=<reason>`` flag — never a
stack trace or a partial session.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from filearr import audit, authx, oidc
from filearr.config import get_settings
from filearr.db import get_session
from filearr.security import set_session_cookie

router = APIRouter()


def _require_enabled() -> None:
    if not get_settings().oidc_is_configured:
        # Fail closed: the feature is off / half-configured — behave as if the
        # route does not exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not enabled")


def _callback_url(request: Request) -> str:
    """The redirect_uri handed to the IdP. Prefers the explicit
    ``FILEARR_OIDC_REDIRECT_URI``; otherwise derives it from the request base
    (honouring the TLS front's X-Forwarded-Proto/Host)."""
    settings = get_settings()
    if settings.oidc_redirect_uri:
        return settings.oidc_redirect_uri
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}/api/v1/auth/oidc/callback"


def _safe_return_to(raw: str | None) -> str:
    """Only a local, single-slash absolute path is allowed as a post-login target
    (blocks open-redirect via ``//evil`` or an absolute URL). Default ``/``."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/auth/oidc/login")
async def oidc_login(
    request: Request,
    return_to: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_enabled()
    try:
        cfg = oidc.OidcConfig.from_settings()
        meta = await oidc.fetch_metadata(cfg)
        state = oidc.random_token()
        nonce = oidc.random_token()
        verifier, challenge = oidc.generate_pkce()
        await oidc.create_login_state(
            session,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            return_to=_safe_return_to(return_to),
        )
        await session.commit()
        url = oidc.build_authorization_url(
            meta,
            cfg,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            redirect_uri=_callback_url(request),
        )
    except oidc.OIDCError as exc:
        await session.rollback()
        return RedirectResponse(
            url="/?" + urlencode({"sso_error": exc.reason}),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    # 303 so the browser issues a GET to the IdP.
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    _require_enabled()

    def _fail(reason: str, return_to: str = "/") -> RedirectResponse:
        sep = "&" if "?" in return_to else "?"
        return RedirectResponse(
            url=f"{return_to}{sep}{urlencode({'sso_error': reason})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if error:
        return _fail(error)
    if not code or not state:
        return _fail("missing_code")

    try:
        cfg = oidc.OidcConfig.from_settings()
        nonce, verifier, return_to = await oidc.consume_login_state(session, state)
        # Persist the state consumption even if a later step fails (single-use).
        await session.commit()
    except oidc.OIDCError as exc:
        await session.rollback()
        return _fail(exc.reason)

    try:
        meta = await oidc.fetch_metadata(cfg)
        tokens = await oidc.exchange_code(
            meta,
            cfg,
            code=code,
            redirect_uri=_callback_url(request),
            code_verifier=verifier,
        )
        jwks = await oidc.fetch_jwks(cfg, meta["jwks_uri"])
        provider = oidc.OIDCProvider(cfg)
        auth_result = provider.authenticate(
            {
                "id_token": tokens["id_token"],
                "access_token": tokens.get("access_token") or "",
                "nonce": nonce,
                "jwks": jwks,
                "signing_algs": oidc.allowed_algs(meta),
            }
        )
        result = await oidc.provision_principal(session, auth_result)
        token = await authx.create_session(
            session,
            result.principal_id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await session.commit()
    except oidc.OIDCError as exc:
        await session.rollback()
        return _fail(exc.reason, _safe_return_to(return_to))

    # A role/group change alters effective grants → invalidate the grant cache so
    # the very next request recomputes (P6-T4).
    if result.role_changed or result.groups_changed:
        from filearr import grant_cache

        grant_cache.bump_generation()

    # P6-T9: audit the SSO login. The OIDC callback is deliberately EXCLUDED from
    # the P6-T8 rate limiter — the 256-bit single-use ``state`` (consumed above)
    # already gates replay/brute force, so there is no credential to lock.
    await audit.emit(
        audit.OIDC_LOGIN, request=request, principal_id=result.principal_id
    )

    response = RedirectResponse(
        url=_safe_return_to(return_to), status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(response, request, token.raw)
    return response
