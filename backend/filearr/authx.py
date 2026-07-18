"""Authentication mechanisms — local passwords, Postgres-backed cookie sessions,
and the federated identity-provider protocol (Phase 6, roadmap §3 /
``docs/research/phase-6-identity-auth-rbac.md`` §1.1-§1.3, §2.1-§2.2).

**P6-T1 implemented here:** argon2id password hashing (``hash_password`` /
``verify_password`` / ``needs_rehash``), the opaque session-token helpers, and
the async Postgres-backed session lifecycle (``create_session`` /
``validate_session`` / ``revoke_session`` / ``revoke_all_for_principal``) plus
the global-role → API-scope mapping (``scopes_for_role``) that lets a session
principal satisfy the existing Bearer-scope dependencies.

**Still stubs (later tasks):** the federated ``AuthProvider`` implementations —
``OIDCProvider`` (P6-T5), ``LDAPProvider`` (P6-T6), ``SAMLProvider`` (P6-T7).
They keep the RBAC layer provider-agnostic: every mechanism converges on
:class:`AuthResult`, so RBAC evaluation never learns which provider produced a
principal. ``LocalPasswordProvider`` stays a typed placeholder — the P6-T1 local
login path is the async ``authenticate_local`` below (verifying against
``users.password_hash``); the provider wrapper is completed alongside
group resolution in P6-T2.

Deliberately NOT decided here (per the brief's rulings): **R5** (the Authlib
floor is re-verified live at P6-T5) and **R4** (external group memberships
resolve at login; admin-tier get a 15-min refresh).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.models import Principal, User
from filearr.models import Session as SessionRow

# --- Local-account password hashing (P6-T1, argon2-cffi) --------------------

# argon2-cffi's ``PasswordHasher`` defaults are Argon2id with RFC 9106 /
# OWASP-aligned parameters (time_cost=3, memory_cost=64 MiB, parallelism=4,
# 16-byte salt, 32-byte tag). This is a DIFFERENT trust model from
# ``security.py``'s API-key sha256-at-rest: API keys are high-entropy CSPRNG
# output (a fast hash is safe); a human password is low-entropy and REQUIRES a
# slow, memory-hard KDF. The two must never share a code path (research §1.1).
_hasher = PasswordHasher()

#: The dummy hash used to keep failed-login timing independent of whether the
#: username exists (anti-enumeration). Computed once at import.
_DUMMY_HASH = _hasher.hash("filearr-nonexistent-user-timing-equalizer")


def hash_password(password: str) -> str:
    """Hash a human-chosen password with Argon2id (P6-T1). Returns the encoded
    argon2 hash string (parameters embedded) for ``users.password_hash``."""
    return _hasher.hash(password)


def verify_password(password: str, encoded_hash: str | None) -> bool:
    """Constant-time verify ``password`` against a stored argon2 hash (P6-T1).

    Returns ``False`` (never raises) on any mismatch or malformed/absent hash.
    When ``encoded_hash`` is ``None`` (a federated-only account with no local
    password) a dummy verify still runs so the timing does not reveal it."""
    target = encoded_hash if encoded_hash is not None else _DUMMY_HASH
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, InvalidHashError, Argon2Error):
        return False
    # A None hash means "no local password" — the verify above only ran to
    # equalize timing; it must still fail closed.
    return encoded_hash is not None


def needs_rehash(encoded_hash: str) -> bool:
    """True if ``encoded_hash`` was produced with weaker parameters than the
    current policy and should be re-hashed on the next successful login."""
    try:
        return _hasher.check_needs_rehash(encoded_hash)
    except (InvalidHashError, Argon2Error):
        return False


# --- Global-role → API-scope mapping (P6-T1) --------------------------------

# A session principal carries a global role; the existing Bearer-key dependencies
# speak in read/write/admin scopes. This maps a role onto the scope set it
# satisfies so a logged-in user transparently passes ``require_scope`` without
# needing an API key (brief §2.2 coexistence table). ADMIN → all; USER →
# write+read; VIEWER → read only.
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": frozenset({"admin", "write", "read"}),
    "user": frozenset({"write", "read"}),
    "viewer": frozenset({"read"}),
}


def scopes_for_role(role: str) -> frozenset[str]:
    """The API scopes a principal with global ``role`` satisfies. Unknown roles
    map to the empty set (fail closed)."""
    return _ROLE_SCOPES.get(role, frozenset())


# --- Session tokens (P6-T1, Postgres-backed, research §1.3 / §2.3) ----------


@dataclass(frozen=True, slots=True)
class SessionToken:
    """The opaque cookie value handed to the browser plus the ``sessions`` row
    key. ``raw`` is shown to the client once (set as the HttpOnly cookie);
    ``session_hash`` (sha256 of ``raw``) is what the row stores — the raw value
    is never persisted, mirroring the API-key pattern."""

    raw: str
    session_hash: str


def hash_session_token(raw: str) -> str:
    """sha256-hex of a raw session token — what ``sessions.session_hash`` stores
    and what a presented cookie is hashed to for the O(1) lookup."""
    return hashlib.sha256(raw.encode()).hexdigest()


def mint_session_token() -> SessionToken:
    """Generate a fresh 256-bit CSPRNG session token (raw + its sha256)."""
    raw = secrets.token_urlsafe(32)
    return SessionToken(raw=raw, session_hash=hash_session_token(raw))


@dataclass(frozen=True, slots=True)
class ValidatedSession:
    """Result of :func:`validate_session`. ``principal_id`` is the owning
    principal. ``session_id`` is the ``sessions`` row PK (stable across token
    rotation) — the auth gate stashes it on ``request.state`` so the session-
    management UI can flag *this* session as the current one (P6-T11). ``rotated``
    carries a freshly-minted token when the 10-minute rotation interval elapsed
    (the caller must re-set the cookie); ``None`` means the existing cookie is
    still current."""

    principal_id: str
    session_id: str
    rotated: SessionToken | None = None


async def create_session(
    session: AsyncSession,
    principal_id: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionToken:
    """Mint a new session row and return its raw token (P6-T1).

    ``expires_absolute = now + FILEARR_SESSION_TTL_HOURS`` (30d default, fixed at
    creation); ``last_seen_at`` starts the 7d inactivity window. Only the sha256
    of the token is persisted; the returned raw value is set as the HttpOnly +
    SameSite=Strict (+ Secure over https) cookie by the caller."""
    settings = get_settings()
    tok = mint_session_token()
    now = datetime.now(UTC)
    row = SessionRow(
        principal_id=principal_id,
        session_hash=tok.session_hash,
        created_at=now,
        last_seen_at=now,
        rotated_at=now,
        expires_absolute=now + timedelta(hours=settings.session_ttl_hours),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()
    return tok


async def validate_session(
    session: AsyncSession, raw_token: str
) -> ValidatedSession | None:
    """Validate a presented cookie token (P6-T1). Returns ``None`` (→ 401) when
    the token is unknown, past its absolute cap, or idle beyond the inactivity
    window; the stale row is deleted in the latter two cases.

    On success it bumps ``last_seen_at`` (sliding inactivity window) and, if the
    rotation interval elapsed, re-keys the row to a fresh token and returns it in
    ``rotated`` so the caller re-sets the cookie. The lookup is by sha256 of the
    high-entropy token (never the raw value), so there is no exploitable timing
    channel on the hash comparison."""
    settings = get_settings()
    token_hash = hash_session_token(raw_token)
    result = await session.execute(
        select(SessionRow).where(SessionRow.session_hash == token_hash)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(UTC)
    if now >= _aware(row.expires_absolute):
        await session.delete(row)
        return None
    if now - _aware(row.last_seen_at) > timedelta(hours=settings.session_inactivity_hours):
        await session.delete(row)
        return None
    rotated: SessionToken | None = None
    if now - _aware(row.rotated_at) >= timedelta(minutes=settings.session_rotation_minutes):
        tok = mint_session_token()
        row.session_hash = tok.session_hash
        row.rotated_at = now
        rotated = tok
    row.last_seen_at = now
    return ValidatedSession(
        principal_id=str(row.principal_id), session_id=str(row.id), rotated=rotated
    )


async def revoke_session(session: AsyncSession, raw_token: str) -> bool:
    """Delete the session row for ``raw_token`` (logout). Idempotent — returns
    True if a row was removed. Invalidation is immediate on the next request."""
    token_hash = hash_session_token(raw_token)
    result = await session.execute(
        delete(SessionRow).where(SessionRow.session_hash == token_hash)
    )
    return bool(result.rowcount)


async def revoke_all_for_principal(session: AsyncSession, principal_id: str) -> int:
    """Kill every session for a principal ("log out everywhere"). Used on a
    privilege change (role edit) and account disable so a change of authority
    takes effect immediately rather than at the next natural rotation."""
    result = await session.execute(
        delete(SessionRow).where(SessionRow.principal_id == principal_id)
    )
    return int(result.rowcount or 0)


def _aware(dt: datetime) -> datetime:
    """Treat a naive timestamp (some drivers return tz-naive) as UTC so the
    lifecycle arithmetic never raises on naive/aware mixing."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- Local username/password login (P6-T1) ----------------------------------


async def authenticate_local(
    session: AsyncSession, username: str, password: str
) -> Principal | None:
    """Verify a local username/password against ``users`` (P6-T1).

    Case-insensitive username match; rejects disabled principals and
    federated-only accounts (NULL ``password_hash``). Runs a dummy verify for an
    unknown user so login timing does not reveal whether the username exists. On
    success, transparently re-hashes the password if the argon2 parameters were
    upgraded and stamps ``last_login_at``. Returns the ``Principal`` or ``None``."""
    normalized = username.strip().lower()
    result = await session.execute(
        select(User, Principal)
        .join(Principal, User.principal_id == Principal.id)
        .where(User.username == normalized)
    )
    row = result.first()
    if row is None:
        # Equalize timing against the existent-user path.
        verify_password(password, None)
        return None
    user, principal = row
    if principal.disabled_at is not None or user.password_hash is None:
        verify_password(password, None)
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = datetime.now(UTC)
    return principal


# --- Federated identity providers (P6-T5 OIDC / P6-T6 LDAP / P6-T7 SAML) ---


@dataclass(frozen=True, slots=True)
class AuthResult:
    """The single downstream artifact every provider resolves to (brief §2.1):
    a stable principal subject plus the external group memberships the RBAC
    layer maps to Filearr ``principal_groups``. ``session_hint`` carries
    provider-specific continuation state (e.g. an OIDC nonce) where needed."""

    external_subject: str
    external_groups: tuple[str, ...] = ()
    session_hint: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class AuthProvider(Protocol):
    """The provider abstraction (brief §2.1). One implementation per mechanism
    (local / ldap / saml / oidc); all converge on :class:`AuthResult` so RBAC
    evaluation has ZERO knowledge of which provider authenticated — the
    load-bearing property that keeps new providers additive.

    ``credentials`` is provider-shaped (username/password for local/ldap; an
    authorization code + state for oidc; a SAMLResponse for saml)."""

    provider_name: str

    def authenticate(self, credentials: dict[str, str]) -> AuthResult:
        """Verify ``credentials`` and return the resolved principal + groups.
        Raises on failure; never returns a partial/unauthenticated result."""
        ...

    def resolve_groups(self, external_subject: str) -> tuple[str, ...]:
        """Re-resolve a principal's external group memberships (R4). Called at
        login for all groups, and on the 15-min background refresh for
        admin-tier memberships only (revocation-risk rationale, open question #5)."""
        ...


class LocalPasswordProvider:
    """local username/password provider placeholder (brief §2.1). The P6-T1
    login path is the async :func:`authenticate_local` above (it needs a DB
    session, which the sync provider Protocol deliberately does not carry); this
    class is completed as the provider wrapper alongside group resolution in
    P6-T2, keeping the federated providers and local login one uniform shape."""

    provider_name = "local"

    def authenticate(self, credentials: dict[str, str]) -> AuthResult:
        raise NotImplementedError(
            "LocalPasswordProvider wrapper lands in P6-T2; P6-T1 login uses "
            "authx.authenticate_local"
        )

    def resolve_groups(self, external_subject: str) -> tuple[str, ...]:
        raise NotImplementedError("LocalPasswordProvider group resolution is P6-T2")


class LDAPProvider:
    """LDAP / AD bind auth — IMPLEMENTED in :mod:`filearr.ldap_auth` (P6-T6).

    Like OIDC, the real login path is not a synchronous ``authenticate`` (it needs
    a DB session for JIT provisioning + a threadpool for the blocking directory
    I/O), so the entry point is the async ``filearr.ldap_auth.authenticate_ldap``,
    invoked by ``/auth/login`` when local auth does not succeed and the username is
    unknown/ldap-sourced. This sync placeholder is kept only so the AuthProvider
    family stays name-complete; it delegates for the pure pieces.

    Library: **ldap3** (pure-Python, offline MOCK_SYNC test harness, no known CVEs
    — a documented override of the research doc's python-ldap preference; see
    ``filearr.ldap_auth`` module docstring). LDAP is the source of truth for group
    membership (an admin never hand-edits an ldap-sourced user's groups, brief
    §2.1)."""

    provider_name = "ldap"

    def authenticate(self, credentials: dict[str, str]) -> AuthResult:
        raise NotImplementedError(
            "LDAP login uses filearr.ldap_auth.authenticate_ldap (async + DB "
            "session), not this sync Protocol placeholder"
        )

    def resolve_groups(self, external_subject: str) -> tuple[str, ...]:
        raise NotImplementedError(
            "LDAP group resolution is filearr.ldap_auth.resolve_ldap_identity"
        )


class SAMLProvider:
    """SP-initiated SAML (P6-T7) — **DEFERRED, not shipped.** Stub retained.

    Ship/defer ruling (2026-07-13, security > integrity): the research-chosen
    library **pysaml2** (latest 7.5.4) hard-pins ``pyopenssl<24.3.0``, which in
    turn requires ``cryptography<44``. Filearr deliberately pins
    ``cryptography==48.0.0`` (the AES-GCM envelope encryption of alert-channel
    secrets, P8-T4). Adopting pysaml2 would FORCE-DOWNGRADE the crypto stack to a
    <44 release — a security regression to satisfy an SP library. The only
    alternative, ``python3-saml``, relies on the in-process libxmlsec1 C extension
    that the research doc explicitly rejected for the xmlsec1-**subprocess**
    isolation posture. Rather than ship a compromised or weak SAML SP, T7 is
    DEFERRED until pysaml2 relaxes the pyopenssl ceiling (or a
    cryptography-48-compatible, subprocess-isolated signature path exists). See
    ``docs/ops/auth.md`` § SAML and the P6-T7 task-doc status. When revisited, the
    XSW defense (reject multi-Assertion responses; bind claims to the signed
    element by ID, brief §4) and the xmlsec1-subprocess timeout remain mandatory."""

    provider_name = "saml"

    def authenticate(self, credentials: dict[str, str]) -> AuthResult:
        raise NotImplementedError("SAMLProvider (P6-T7) is DEFERRED — see docstring")

    def resolve_groups(self, external_subject: str) -> tuple[str, ...]:
        raise NotImplementedError("SAMLProvider (P6-T7) is DEFERRED — see docstring")


# ``OIDCProvider`` is IMPLEMENTED in ``filearr.oidc`` (P6-T5) — the real RP needs
# async HTTP + a DB session, which the sync stub Protocol cannot carry. It is
# re-exported here lazily so ``authx.OIDCProvider`` remains the canonical name
# (matching the AuthProvider family) without a circular import at module load
# (``filearr.oidc`` imports ``AuthResult`` from this module).
def __getattr__(name: str):  # pragma: no cover - trivial lazy re-export
    if name == "OIDCProvider":
        from filearr.oidc import OIDCProvider

        return OIDCProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
