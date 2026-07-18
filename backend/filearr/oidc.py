"""OIDC / OpenID-Connect relying-party (RP) implementation (Phase 6, P6-T5).

The first federation option (brief T24 / ``docs/research/phase-6-identity-auth-
rbac.md`` §1.1). Authorization-code flow **with PKCE (S256)**, opaque
server-side ``state``/``nonce``, and ID-token validation (signature via JWKS +
``iss``/``aud``/``exp``/``nonce`` + explicit ``at_hash`` binding — the
CVE-2026-28498 fail-open class). A successful login MINTS THE STANDARD P6-T1
Postgres session (``authx.create_session``) — there is deliberately NO parallel
auth path; every mechanism converges on a plain session cookie.

Security posture:
* **Authlib pin re-verified live at implementation (R5):** the newest advisory
  (GHSA-w8p2-r796-3vmq, 2026-06-08) is patched only in 1.6.10/1.7.1, so the floor
  moved UP from the brief's ``>=1.6.9`` to ``>=1.7.1`` (pyproject.toml).
* **Signature algorithms are allow-listed to asymmetric families** (never
  ``none``, never HMAC) so the ``alg:none`` (GHSA-7wc2-qxgw-g8gg) and
  algorithm-confusion (CVE-2024-37568) classes cannot reach us.
* **SSRF bounded:** the issuer is operator-config (not user input); every fetched
  URL is still required to be https (or loopback for dev), and discovery/JWKS/
  token responses are read under a byte cap + timeout.
* **Fail-closed:** any validation, provisioning, or config error raises
  :class:`OIDCError` — the caller redirects to the login page with a generic
  message; a partial/unauthenticated principal is never produced.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import Settings, get_settings

# Authlib's ``authlib.jose`` is deprecated in favour of joserfc but remains the
# supported, CVE-patched API through the 1.x line (compat guaranteed before 2.0);
# the deprecation warning is noise here.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebToken
    from authlib.jose.errors import JoseError
    from authlib.oidc.core import CodeIDToken

# Authlib 1.7 delegates JOSE crypto to ``joserfc``; the errors raised at
# decode/validate time are ``joserfc.errors.*`` (NOT subclasses of Authlib's own
# ``JoseError``). Catch both bases so EVERY validation failure funnels into a
# fail-closed :class:`OIDCError`.
try:
    from joserfc.errors import JoseError as _JoseRfcError

    _JOSE_ERRORS: tuple = (JoseError, _JoseRfcError, ValueError, KeyError, TypeError)
except Exception:  # pragma: no cover - joserfc always present with Authlib 1.7+
    _JOSE_ERRORS = (JoseError, ValueError, KeyError, TypeError)

# Asymmetric signature families only — ``none`` and HMAC are NEVER accepted (they
# are the alg:none / HMAC-confusion bypass classes). Intersected with the IdP's
# advertised ``id_token_signing_alg_values_supported`` at request time.
ALLOWED_SIGNING_ALGS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)

_GLOBAL_ROLE_RANK = {"viewer": 0, "user": 1, "admin": 2}


class OIDCError(Exception):
    """Any OIDC login failure. ``reason`` is a short machine token surfaced (only)
    as a generic query flag to the SPA — never the raw exception detail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


# --------------------------------------------------------------------------- #
# Config snapshot                                                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OidcConfig:
    """Immutable snapshot of the FILEARR_OIDC_* settings for one flow."""

    issuer: str
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    redirect_uri: str | None
    role_claim: str | None
    role_map: dict[str, str]
    default_role: str
    auto_provision: bool
    username_claim: str
    link_by_email: bool
    group_claim: str | None
    state_ttl_minutes: int
    http_timeout_s: float
    discovery_max_bytes: int
    metadata_cache_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> OidcConfig:
        s = settings or get_settings()
        if not (s.oidc_issuer and s.oidc_client_id):
            raise OIDCError("not_configured", "OIDC issuer/client_id are unset")
        issuer = s.oidc_issuer.rstrip("/")
        _require_safe_url(issuer)
        return cls(
            issuer=issuer,
            client_id=s.oidc_client_id,
            client_secret=s.oidc_client_secret,
            scopes=tuple(s.oidc_scope_list),
            redirect_uri=s.oidc_redirect_uri,
            role_claim=s.oidc_role_claim,
            role_map=s.oidc_role_map_parsed,
            default_role=s.oidc_default_role.strip().lower(),
            auto_provision=s.oidc_auto_provision,
            username_claim=s.oidc_username_claim,
            link_by_email=s.oidc_link_by_email,
            group_claim=s.oidc_group_claim,
            state_ttl_minutes=s.oidc_login_state_ttl_minutes,
            http_timeout_s=s.oidc_http_timeout_s,
            discovery_max_bytes=s.oidc_discovery_max_bytes,
            metadata_cache_seconds=s.oidc_metadata_cache_seconds,
        )


def _require_safe_url(url: str) -> None:
    """Reject anything that is not https, except explicit loopback for local dev
    (bounds the SSRF blast radius even though the issuer is operator-trusted)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return
    raise OIDCError("insecure_url", f"OIDC URL must be https (or loopback): {url}")


# --------------------------------------------------------------------------- #
# PKCE / state / nonce                                                        #
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def random_token() -> str:
    """A 256-bit CSPRNG url-safe token (state / nonce)."""
    return secrets.token_urlsafe(32)


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256 (RFC 7636)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --------------------------------------------------------------------------- #
# Discovery + JWKS (cached, capped, timed out)                                #
# --------------------------------------------------------------------------- #
# issuer/url -> (expires_monotonic, payload)
_meta_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> dict | None:
    hit = _meta_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    return None


def _cache_put(key: str, payload: dict, ttl: float) -> None:
    _meta_cache[key] = (time.monotonic() + ttl, payload)


def clear_caches() -> None:
    """Drop the discovery/JWKS caches (used by tests + on config reload)."""
    _meta_cache.clear()


async def _get_json_capped(url: str, cfg: OidcConfig) -> dict:
    """GET ``url`` and parse JSON, enforcing the response-size cap + timeout and
    the https/loopback constraint. Used for discovery + JWKS."""
    import json

    _require_safe_url(url)
    try:
        async with httpx.AsyncClient(timeout=cfg.http_timeout_s) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > cfg.discovery_max_bytes:
                        raise OIDCError("oversized_response", f"{url} exceeded cap")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise OIDCError("fetch_failed", f"{url}: {exc}") from exc
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OIDCError("bad_response", f"{url}: not JSON") from exc


async def fetch_metadata(cfg: OidcConfig) -> dict:
    """Fetch + cache the OIDC discovery document. Validates that the returned
    ``issuer`` matches the configured one (spec-mandated) and that the endpoints
    we will call are https/loopback."""
    cached = _cache_get(f"meta:{cfg.issuer}")
    if cached is not None:
        return cached
    url = f"{cfg.issuer}/.well-known/openid-configuration"
    meta = await _get_json_capped(url, cfg)
    if meta.get("issuer", "").rstrip("/") != cfg.issuer:
        raise OIDCError("issuer_mismatch", "discovery issuer != configured issuer")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        val = meta.get(key)
        if not val:
            raise OIDCError("incomplete_discovery", f"missing {key}")
        _require_safe_url(val)
    _cache_put(f"meta:{cfg.issuer}", meta, cfg.metadata_cache_seconds)
    return meta


async def fetch_jwks(cfg: OidcConfig, jwks_uri: str) -> dict:
    """Fetch + cache the IdP's JWKS (the signing keys for ID-token verification)."""
    cached = _cache_get(f"jwks:{jwks_uri}")
    if cached is not None:
        return cached
    jwks = await _get_json_capped(jwks_uri, cfg)
    if not jwks.get("keys"):
        raise OIDCError("empty_jwks", "JWKS has no keys")
    _cache_put(f"jwks:{jwks_uri}", jwks, cfg.metadata_cache_seconds)
    return jwks


def allowed_algs(meta: dict) -> list[str]:
    """The signature algs we will accept = the IdP's advertised set ∩ our
    asymmetric allow-list (defaults to RS256 if the IdP advertises nothing)."""
    advertised = meta.get("id_token_signing_alg_values_supported") or ["RS256"]
    algs = [a for a in advertised if a in ALLOWED_SIGNING_ALGS]
    if not algs:
        raise OIDCError("no_supported_alg", "IdP offers no acceptable signing alg")
    return algs


def build_authorization_url(
    meta: dict, cfg: OidcConfig, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
) -> str:
    """Compose the redirect URL to the IdP authorization endpoint (code + PKCE)."""
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(cfg.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(
    meta: dict, cfg: OidcConfig, *, code: str, redirect_uri: str, code_verifier: str
) -> dict:
    """Exchange the authorization ``code`` for tokens at the token endpoint.

    Client authentication: ``client_secret_post`` when a secret is configured,
    else a public client (PKCE-only). Returns the token response dict (must carry
    ``id_token``)."""
    token_endpoint = meta["token_endpoint"]
    _require_safe_url(token_endpoint)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": code_verifier,
    }
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    try:
        async with httpx.AsyncClient(timeout=cfg.http_timeout_s) as client:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OIDCError("token_exchange_failed", str(exc)) from exc
    if resp.status_code != 200:
        raise OIDCError("token_exchange_failed", f"token endpoint {resp.status_code}")
    if len(resp.content) > cfg.discovery_max_bytes:
        raise OIDCError("oversized_response", "token response exceeded cap")
    try:
        tokens = resp.json()
    except ValueError as exc:
        raise OIDCError("bad_response", "token response not JSON") from exc
    if not tokens.get("id_token"):
        raise OIDCError("no_id_token", "token response missing id_token")
    return tokens


# --------------------------------------------------------------------------- #
# The provider (re-exported as ``authx.OIDCProvider``)                        #
# --------------------------------------------------------------------------- #
class OIDCProvider:
    """OIDC RP provider (P6-T5). Converges on :class:`authx.AuthResult` like every
    other federation mechanism, so the RBAC layer never learns SSO was involved.

    The heavy web orchestration (redirect, code exchange, JWKS) lives in the
    module functions above + ``api/oidc.py``; :meth:`authenticate` is the pure,
    synchronous ID-token *validation* core (the security-critical part) and
    :meth:`resolve_groups` the pure claim→group extraction."""

    provider_name = "oidc"

    def __init__(self, config: OidcConfig | None = None) -> None:
        self.config = config or OidcConfig.from_settings()

    def resolve_groups(self, claims: dict) -> tuple[str, ...]:
        """External IdP group values from the configured group claim (login-time
        resolution, R4). ``()`` when no group claim is configured/present."""
        cfg = self.config
        if not cfg.group_claim:
            return ()
        raw = claims.get(cfg.group_claim)
        if raw is None:
            return ()
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        return tuple(str(v) for v in values if v is not None and str(v) != "")

    def resolve_role(self, claims: dict) -> str | None:
        """Map claims → a Filearr global role, evaluated at EVERY login. Returns
        ``None`` to REFUSE the login (unmapped + empty default_role = fail-closed).
        The highest-privilege mapped value wins when several match."""
        cfg = self.config
        matched: list[str] = []
        if cfg.role_claim:
            raw = claims.get(cfg.role_claim)
            values = raw if isinstance(raw, (list, tuple)) else ([raw] if raw is not None else [])
            for v in values:
                mapped = cfg.role_map.get(str(v))
                if mapped:
                    matched.append(mapped)
        if matched:
            return max(matched, key=lambda r: _GLOBAL_ROLE_RANK.get(r, -1))
        return cfg.default_role or None  # empty default => refuse (fail-closed)

    def authenticate(self, credentials: dict) -> object:
        """Validate an ID token and return an :class:`authx.AuthResult`.

        ``credentials`` keys: ``id_token``, ``access_token`` (may be ""),
        ``nonce``, ``jwks`` (dict), ``signing_algs`` (list). Raises
        :class:`OIDCError` on ANY tamper — bad signature, wrong ``iss``/``aud``,
        expired, ``nonce`` mismatch, or ``at_hash`` mismatch (CVE-2026-28498
        fail-open class — we pass the access token so the binding is CHECKED, and
        additionally require ``at_hash`` presence when an access token exists)."""
        from filearr.authx import AuthResult

        cfg = self.config
        id_token = credentials.get("id_token")
        if not id_token:
            raise OIDCError("no_id_token", "missing id_token")
        access_token = credentials.get("access_token") or ""
        nonce = credentials.get("nonce") or ""
        jwks = credentials.get("jwks") or {}
        algs = credentials.get("signing_algs") or ["RS256"]
        jwt = JsonWebToken([a for a in algs if a in ALLOWED_SIGNING_ALGS] or ["RS256"])
        claims_options = {
            "iss": {"essential": True, "value": cfg.issuer},
            "aud": {"essential": True, "value": cfg.client_id},
        }
        claims_params = {"nonce": nonce, "access_token": access_token}
        try:
            claims = jwt.decode(
                id_token,
                jwks,
                claims_cls=CodeIDToken,
                claims_options=claims_options,
                claims_params=claims_params,
            )
            claims.validate(leeway=120)
        except _JOSE_ERRORS as exc:
            raise OIDCError("invalid_id_token", f"{type(exc).__name__}: {exc}") from exc
        # Defence-in-depth over Authlib's own at_hash check: when an access token
        # was issued, REQUIRE the binding claim to be present (fail-closed) — an
        # IdP that drops at_hash must not silently skip the binding.
        if access_token and not claims.get("at_hash"):
            raise OIDCError("missing_at_hash", "access_token present without at_hash")

        subject = claims.get("sub")
        if not subject:
            raise OIDCError("no_subject", "id_token missing sub")
        role = self.resolve_role(claims)
        if role is None:
            raise OIDCError("no_role", "user matched no role and default_role is empty")
        hint = {
            "issuer": cfg.issuer,
            "role": role,
            "email": str(claims.get("email") or ""),
            "email_verified": "true" if claims.get("email_verified") is True else "false",
            "preferred_username": str(
                claims.get(cfg.username_claim) or claims.get("preferred_username") or ""
            ),
            "name": str(claims.get("name") or ""),
        }
        return AuthResult(
            external_subject=str(subject),
            external_groups=self.resolve_groups(claims),
            session_hint=hint,
        )


# --------------------------------------------------------------------------- #
# Login-state persistence (single-use, TTL)                                   #
# --------------------------------------------------------------------------- #
async def create_login_state(
    session: AsyncSession, *, state: str, nonce: str, code_verifier: str, return_to: str
) -> None:
    """Persist a login-state row and opportunistically sweep expired ones."""
    from filearr.models import OidcLoginState

    cfg_ttl = get_settings().oidc_login_state_ttl_minutes
    cutoff = datetime.now(UTC) - timedelta(minutes=cfg_ttl)
    await session.execute(delete(OidcLoginState).where(OidcLoginState.created_at < cutoff))
    session.add(
        OidcLoginState(
            state=state, nonce=nonce, code_verifier=code_verifier, return_to=return_to
        )
    )
    await session.flush()


async def consume_login_state(session: AsyncSession, state: str):
    """Load-and-DELETE a login-state row (single use). Returns the row (detached
    values) or raises :class:`OIDCError` when missing or past its TTL."""
    from filearr.models import OidcLoginState

    row = (
        await session.execute(select(OidcLoginState).where(OidcLoginState.state == state))
    ).scalar_one_or_none()
    if row is None:
        raise OIDCError("bad_state", "unknown or already-used state")
    nonce, verifier, return_to, created = (
        row.nonce,
        row.code_verifier,
        row.return_to,
        row.created_at,
    )
    await session.delete(row)
    await session.flush()
    ttl = get_settings().oidc_login_state_ttl_minutes
    created_aware = created if created.tzinfo else created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created_aware > timedelta(minutes=ttl):
        raise OIDCError("expired_state", "login state expired")
    return nonce, verifier, return_to


# --------------------------------------------------------------------------- #
# JIT provisioning / linking / role + group sync                             #
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ProvisionResult:
    principal_id: str
    role_changed: bool
    groups_changed: bool


async def _unique_username(session: AsyncSession, base: str) -> str:
    """A collision-safe lower-cased username (shared with LDAP — see
    :mod:`filearr.provisioning`)."""
    from filearr.provisioning import unique_username

    return await unique_username(session, base)


async def _sync_groups(
    session: AsyncSession, principal_id, idp_groups: tuple[str, ...]
) -> bool:
    """Reconcile the principal's groups against the IdP group values (P6-T5).

    Delegates to the shared :func:`filearr.provisioning.sync_external_groups`
    with ``source='oidc'``: a name-matching ``principal_groups`` row is joined
    regardless of source; removal is scoped to ``source='oidc'`` rows only (a
    ``source='local'`` membership — an admin's manual grant — is never
    clobbered). Returns True if anything changed (→ caller bumps the grant
    cache)."""
    from filearr.provisioning import sync_external_groups

    return await sync_external_groups(
        session, principal_id, idp_groups, source="oidc"
    )

async def provision_principal(session: AsyncSession, auth_result) -> ProvisionResult:
    """Resolve an :class:`authx.AuthResult` to a Filearr principal (P6-T5).

    Order: (1) existing federated identity → update role + groups; (2) email-link
    when ``FILEARR_OIDC_LINK_BY_EMAIL`` and an EXACT verified-email match; (3) JIT
    provision when ``FILEARR_OIDC_AUTO_PROVISION``; else REFUSE (fail-closed). A
    disabled principal is refused. Role mapping is applied on EVERY login."""
    from filearr.models import Principal, User

    settings = get_settings()
    hint = auth_result.session_hint
    issuer = hint["issuer"]
    subject = auth_result.external_subject
    role = hint["role"]

    # (1) existing linked identity
    row = (
        await session.execute(
            select(User, Principal)
            .join(Principal, Principal.id == User.principal_id)
            .where(
                User.auth_provider == "oidc",
                User.external_issuer == issuer,
                User.external_subject == subject,
            )
        )
    ).first()

    principal: Principal | None = None
    user: User | None = None
    if row is not None:
        user, principal = row
    elif settings.oidc_link_by_email and hint.get("email") and hint.get("email_verified") == "true":
        # (2) exact verified-email link (account-takeover surface — default off)
        email = hint["email"].strip().lower()
        cand = (
            await session.execute(
                select(User, Principal)
                .join(Principal, Principal.id == User.principal_id)
                .where(User.auth_provider == "local")
            )
        ).all()
        for u, p in cand:
            if (u.email or "").strip().lower() == email:
                user, principal = u, p
                user.auth_provider = "oidc"
                user.external_issuer = issuer
                user.external_subject = subject
                break

    if principal is None:
        # (3) JIT provision or refuse
        if not settings.oidc_auto_provision:
            raise OIDCError("no_account", "no linked account and auto-provision is off")
        base = hint.get("preferred_username") or (hint.get("email") or "").split("@")[0] or subject
        username = await _unique_username(session, base)
        principal = Principal(kind="user", global_role=role)
        session.add(principal)
        await session.flush()
        user = User(
            principal_id=principal.id,
            username=username,
            email=hint.get("email") or None,
            password_hash=None,  # SSO-only account: local password login refused
            auth_provider="oidc",
            external_issuer=issuer,
            external_subject=subject,
        )
        session.add(user)
        await session.flush()

    if principal.disabled_at is not None:
        raise OIDCError("disabled", "account is disabled")

    # Role mapping applied every login.
    role_changed = principal.global_role != role
    if role_changed:
        principal.global_role = role
    user.last_login_at = datetime.now(UTC)

    groups_changed = await _sync_groups(session, principal.id, auth_result.external_groups)
    return ProvisionResult(
        principal_id=str(principal.id),
        role_changed=role_changed,
        groups_changed=groups_changed,
    )
