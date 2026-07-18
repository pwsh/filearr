"""LDAP / Active Directory bind authentication (Phase 6, P6-T6).

The second federation option (brief T25 / ``docs/research/phase-6-identity-auth-
rbac.md`` §2.1). Converges on the same downstream artifacts as OIDC — a stable
external subject + a set of group memberships + a mapped global role — so the
RBAC layer never learns which provider authenticated (:mod:`filearr.provisioning`
is shared).

Library choice — **ldap3**, a deliberate override of the research doc's
python-ldap preference (documented in ``docs/ops/auth.md``): ldap3 is pure-Python
(no C toolchain / libldap headers), carries no known CVEs (checked live
2026-07-13), and — decisively — ships an offline ``MOCK_SYNC`` strategy that lets
the security-critical injection / bind / group-mapping matrix run network-free in
CI. python-ldap has no offline harness (it needs a live directory), so those
tests could not exist. LDAPv3 bind/search is a frozen protocol, so a frozen
(2.9.1, 2021) but un-vulnerable client is an acceptable maintenance risk. The
directory I/O is isolated behind :func:`connect`, so a future swap is localized.

Security posture:
* **TLS-first, never silent plaintext.** ``ldaps://`` uses implicit TLS; a
  non-loopback ``ldap://`` is upgraded with StartTLS (default) and REFUSED
  outright without it unless the operator explicitly opts into plaintext (logged
  loudly). Server-cert verification is on by default.
* **The user's password is verified ONLY by a real bind** — never by a filter
  trick or an attribute compare. An empty password is rejected BEFORE any socket
  is opened (RFC 4513 unauthenticated-bind / anonymous-bind class).
* **LDAP injection closed:** every user-supplied value is
  ``escape_filter_chars``-escaped into filters and DN-escaped into DN templates.
* **Referrals off** (``auto_referrals=False``) so a hostile/misconfigured server
  cannot bounce the bind elsewhere; **read-only** connections; hard connect +
  receive timeouts.
* **Fail-closed:** any config/transport/validation error raises
  :class:`LDAPError`; the login path treats it as an auth failure (generic 401).
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ldap3 import ANONYMOUS, BASE, NONE, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import Settings, get_settings

logger = logging.getLogger("filearr.ldap")

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "", None}
_GLOBAL_ROLE_RANK = {"viewer": 0, "user": 1, "admin": 2}


class LDAPError(Exception):
    """Any LDAP login failure. ``reason`` is a short machine token; the raw detail
    is never surfaced to the client (the login path maps it to a generic 401)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


# --------------------------------------------------------------------------- #
# Config snapshot                                                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LdapConfig:
    server: str
    scheme: str
    host: str
    port: int
    transport: str  # 'ldaps' | 'starttls' | 'plaintext'
    tls_verify: bool
    tls_ca_cert_file: str | None
    timeout: int
    bind_dn: str | None
    bind_password: str | None
    user_dn_template: str | None
    user_base: str | None
    user_filter: str
    attr_username: str
    attr_email: str
    attr_uid: str
    use_memberof: bool
    attr_memberof: str
    group_base: str | None
    group_filter: str
    role_map: dict[str, str]
    default_role: str
    auto_provision: bool
    group_sync: bool

    @property
    def issuer(self) -> str:
        """The stable identity scope for an LDAP subject = the server host
        (mirrors OIDC's issuer). Identity = (ldap, issuer, subject)."""
        return self.host.lower()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LdapConfig:
        s = settings or get_settings()
        if not (s.ldap_enabled and s.ldap_server):
            raise LDAPError("not_configured", "LDAP server is unset")
        if not (s.ldap_user_dn_template or s.ldap_user_base):
            raise LDAPError("not_configured", "LDAP needs user_dn_template or user_base")
        parts = urlsplit(s.ldap_server)
        scheme = (parts.scheme or "ldap").lower()
        if scheme not in ("ldap", "ldaps"):
            raise LDAPError("bad_server_url", f"unsupported scheme: {scheme}")
        host = parts.hostname or ""
        use_ssl = scheme == "ldaps"
        port = parts.port or (636 if use_ssl else 389)
        is_loopback = host.lower() in _LOOPBACK
        # Transport policy — never silent plaintext to a remote host.
        if use_ssl:
            transport = "ldaps"
        elif is_loopback:
            transport = "plaintext"  # local dev
        elif s.ldap_start_tls:
            transport = "starttls"
        elif s.ldap_allow_plaintext:
            transport = "plaintext"
            logger.warning(
                "LDAP: plaintext ldap:// to non-loopback host %s (StartTLS off, "
                "ldap_allow_plaintext=true) — credentials cross the wire "
                "unprotected. Use ldaps:// or ldap_start_tls=true.",
                host,
            )
        else:
            raise LDAPError(
                "insecure_transport",
                "refusing plaintext ldap:// to a non-loopback host without "
                "StartTLS; set an ldaps:// server, ldap_start_tls=true, or "
                "(discouraged) ldap_allow_plaintext=true",
            )
        if transport in ("ldaps", "starttls") and not s.ldap_tls_verify:
            logger.warning(
                "LDAP: TLS certificate verification is DISABLED "
                "(ldap_tls_verify=false) — MITM protection is off. Provide "
                "ldap_tls_ca_cert_file and re-enable verification in production."
            )
        return cls(
            server=s.ldap_server,
            scheme=scheme,
            host=host,
            port=port,
            transport=transport,
            tls_verify=s.ldap_tls_verify,
            tls_ca_cert_file=s.ldap_tls_ca_cert_file,
            timeout=s.ldap_timeout,
            bind_dn=(s.ldap_bind_dn or None),
            bind_password=(s.ldap_bind_password or None),
            user_dn_template=(s.ldap_user_dn_template or None),
            user_base=(s.ldap_user_base or None),
            user_filter=s.ldap_user_filter,
            attr_username=s.ldap_attr_username,
            attr_email=s.ldap_attr_email,
            attr_uid=s.ldap_attr_uid,
            use_memberof=s.ldap_use_memberof,
            attr_memberof=s.ldap_attr_memberof,
            group_base=(s.ldap_group_base or None),
            group_filter=s.ldap_group_filter,
            role_map=s.ldap_role_map_parsed,
            default_role=s.ldap_default_role.strip().lower(),
            auto_provision=s.ldap_auto_provision,
            group_sync=s.ldap_group_sync,
        )


# --------------------------------------------------------------------------- #
# Resolved identity                                                           #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LdapIdentity:
    subject: str
    username: str
    email: str | None
    group_dns: tuple[str, ...] = ()
    group_names: tuple[str, ...] = ()


@dataclass(slots=True)
class ProvisionResult:
    principal_id: str
    role_changed: bool
    groups_changed: bool


# --------------------------------------------------------------------------- #
# Connection factory (the sole directory-I/O seam — monkeypatched in tests)   #
# --------------------------------------------------------------------------- #
def _escape_dn_value(value: str) -> str:
    """Escape a single value for safe interpolation into a DN component (RFC 4514
    special chars) — used for ``user_dn_template``."""
    out = []
    for i, ch in enumerate(value):
        if ch in '\\,+"<>;=' or ch == "\x00":
            out.append("\\" + ch)
        elif ch == " " and (i == 0 or i == len(value) - 1):
            out.append("\\ ")
        elif ch == "#" and i == 0:
            out.append("\\#")
        else:
            out.append(ch)
    return "".join(out)


def connect(cfg: LdapConfig, *, user: str | None, password: str | None) -> Connection | None:
    """Open a TLS-appropriate connection and attempt a SIMPLE (or ANONYMOUS)
    bind. Returns the bound :class:`Connection`, or ``None`` when the bind was
    REJECTED (bad credentials). Raises :class:`LDAPError` on any transport /
    StartTLS / socket failure. The single point where real sockets are opened —
    tests inject an offline ``MOCK_SYNC`` connector in its place."""
    use_ssl = cfg.transport == "ldaps"
    tls = None
    if cfg.transport in ("ldaps", "starttls"):
        tls = Tls(
            validate=ssl.CERT_REQUIRED if cfg.tls_verify else ssl.CERT_NONE,
            ca_certs_file=cfg.tls_ca_cert_file,
            version=ssl.PROTOCOL_TLS_CLIENT if cfg.tls_verify else ssl.PROTOCOL_TLS,
        )
    server = Server(
        cfg.host, port=cfg.port, use_ssl=use_ssl, tls=tls, get_info=NONE,
        connect_timeout=cfg.timeout,
    )
    auth = SIMPLE if user else ANONYMOUS
    try:
        conn = Connection(
            server,
            user=user or None,
            password=password or None,
            authentication=auth,
            auto_referrals=False,       # never chase referrals
            receive_timeout=cfg.timeout,
            raise_exceptions=False,
            read_only=True,
        )
        conn.open()
        if cfg.transport == "starttls" and not conn.start_tls():
            raise LDAPError("starttls_failed", "StartTLS upgrade was refused")
        if not conn.bind():
            _safe_unbind(conn)
            return None
    except LDAPError:
        raise
    except LDAPException as exc:  # socket/TLS/protocol failure
        raise LDAPError("connection_failed", str(exc)) from exc
    return conn


def _safe_unbind(conn) -> None:
    try:
        if conn is not None:
            conn.unbind()
    except Exception:  # pragma: no cover - cleanup best-effort
        pass


# --------------------------------------------------------------------------- #
# Identity resolution (blocking — run in a threadpool by authenticate_ldap)   #
# --------------------------------------------------------------------------- #
def _attrs_dict(entry) -> dict:
    try:
        return dict(entry.entry_attributes_as_dict)
    except Exception:  # pragma: no cover
        return {}


def _first(values) -> str | None:
    if not values:
        return None
    v = values[0] if isinstance(values, (list, tuple)) else values
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.hex()
    return str(v)


def _dn_cn(dn: str) -> str:
    """The group's short name (its ``cn`` RDN, else the first RDN value) for the
    name-match ``principal_groups`` sync."""
    try:
        rdns = parse_dn(dn)
    except Exception:
        return dn
    for attr, value, _sep in rdns:
        if attr.lower() == "cn":
            return value
    return rdns[0][1] if rdns else dn


def _bind_service(cfg: LdapConfig, connector) -> Connection:
    """Bind the search/reader identity (the service account, or anonymous when no
    bind_dn is configured). Raises when the bind is refused."""
    conn = connector(cfg, user=cfg.bind_dn or "", password=cfg.bind_password or "")
    if conn is None:
        raise LDAPError("service_bind_failed", "service/anonymous search bind refused")
    return conn


def _search_user(cfg: LdapConfig, reader: Connection, username: str):
    """Search-then-bind step 1: locate exactly one user entry. Returns the entry
    or ``None`` (unknown / ambiguous)."""
    filt = cfg.user_filter.format(username=escape_filter_chars(username))
    attrs = [cfg.attr_username, cfg.attr_email, cfg.attr_uid]
    if cfg.use_memberof:
        attrs.append(cfg.attr_memberof)
    try:
        reader.search(
            cfg.user_base, filt, search_scope=SUBTREE, attributes=attrs, size_limit=2
        )
    except LDAPException as exc:
        raise LDAPError("search_failed", str(exc)) from exc
    entries = list(reader.entries)
    if len(entries) != 1:
        return None  # 0 = unknown, >1 = ambiguous filter → refuse
    return entries[0]


def _read_entry(cfg: LdapConfig, reader: Connection, dn: str):
    attrs = [cfg.attr_username, cfg.attr_email, cfg.attr_uid]
    if cfg.use_memberof:
        attrs.append(cfg.attr_memberof)
    try:
        reader.search(dn, "(objectClass=*)", search_scope=BASE, attributes=attrs)
        entries = list(reader.entries)
        return entries[0] if entries else None
    except LDAPException:
        return None


def _extract_attrs(cfg: LdapConfig, entry, user_dn: str, fallback_username: str):
    attrs = _attrs_dict(entry) if entry is not None else {}
    subject = _first(attrs.get(cfg.attr_uid))
    if not subject:
        subject = user_dn
        logger.warning(
            "LDAP: user %s has no %s attribute — falling back to the DN as the "
            "stable subject (a rename will orphan the account). Configure "
            "ldap_attr_uid (entryUUID / objectGUID).",
            user_dn, cfg.attr_uid,
        )
    username = _first(attrs.get(cfg.attr_username)) or fallback_username
    email = _first(attrs.get(cfg.attr_email))
    return subject, username, email


def _resolve_groups(cfg: LdapConfig, reader: Connection, entry, user_dn: str) -> tuple[str, ...]:
    if cfg.use_memberof:
        attrs = _attrs_dict(entry) if entry is not None else {}
        vals = attrs.get(cfg.attr_memberof) or []
        return tuple(str(v) for v in vals if v)
    if not cfg.group_base:
        return ()
    filt = cfg.group_filter.format(user_dn=escape_filter_chars(user_dn))
    try:
        reader.search(cfg.group_base, filt, search_scope=SUBTREE, attributes=["cn"])
        return tuple(e.entry_dn for e in reader.entries)
    except LDAPException as exc:
        raise LDAPError("group_search_failed", str(exc)) from exc


def resolve_ldap_identity(
    cfg: LdapConfig, username: str, password: str, *, connector=connect
) -> LdapIdentity | None:
    """Verify credentials against the directory and return the resolved identity,
    or ``None`` on auth failure. Blocking — called via a threadpool.

    Order: reject empty creds (never bind anonymously as the user) → locate the
    user DN (template or search-then-bind) → BIND as the user with the presented
    password (the only password check) → read attributes + groups with the
    service/reader connection."""
    username = (username or "").strip()
    if not username or not password:
        # Empty password ⇒ never attempt a bind (RFC 4513 unauthenticated /
        # anonymous-bind class would otherwise "succeed" and impersonate).
        return None

    conns: list[Connection] = []
    try:
        # A reader (service/anonymous) connection is needed for a search-mode
        # lookup, and for attribute/group reads whenever a service account exists.
        svc: Connection | None = None
        if cfg.bind_dn is not None or not cfg.user_dn_template:
            svc = _bind_service(cfg, connector)
            conns.append(svc)

        entry = None
        if cfg.user_dn_template:
            user_dn = cfg.user_dn_template.format(username=_escape_dn_value(username))
        else:
            entry = _search_user(cfg, svc, username)
            if entry is None:
                return None
            user_dn = entry.entry_dn

        # THE password check: bind as the user.
        user_conn = connector(cfg, user=user_dn, password=password)
        if user_conn is None:
            return None
        conns.append(user_conn)

        reader = svc if svc is not None else user_conn
        if entry is None:
            entry = _read_entry(cfg, reader, user_dn)

        subject, uname, email = _extract_attrs(cfg, entry, user_dn, username)
        group_dns = _resolve_groups(cfg, reader, entry, user_dn)
        group_names = tuple(_dn_cn(dn) for dn in group_dns)
        return LdapIdentity(
            subject=subject,
            username=uname,
            email=email,
            group_dns=group_dns,
            group_names=group_names,
        )
    finally:
        for c in conns:
            _safe_unbind(c)


# --------------------------------------------------------------------------- #
# Role mapping + JIT provisioning                                            #
# --------------------------------------------------------------------------- #
def resolve_role(cfg: LdapConfig, group_dns: tuple[str, ...]) -> str | None:
    """Map the user's group DNs → a Filearr global role (evaluated EVERY login).
    Highest-privilege match wins; returns ``None`` to REFUSE when nothing matches
    and ``ldap_default_role`` is empty (fail-closed)."""
    matched: list[str] = []
    for dn in group_dns:
        role = cfg.role_map.get(dn.lower())
        if role:
            matched.append(role)
    if matched:
        return max(matched, key=lambda r: _GLOBAL_ROLE_RANK.get(r, -1))
    return cfg.default_role or None


async def provision_ldap_principal(
    session: AsyncSession, cfg: LdapConfig, identity: LdapIdentity
) -> ProvisionResult:
    """Resolve an :class:`LdapIdentity` to a Filearr principal (P6-T6).

    Identity = (auth_provider='ldap', external_issuer=<server host>,
    external_subject=<entryUUID/objectGUID or DN>). No email-linking (LDAP is
    typically the same directory as local accounts — an unrequested surface);
    JIT-provisions an SSO-only user (NULL password_hash) when auto_provision is
    on, else refuses. Role mapping + group sync applied on every login."""
    from filearr.models import Principal, User
    from filearr.provisioning import sync_external_groups, unique_username

    role = resolve_role(cfg, identity.group_dns)
    if role is None:
        raise LDAPError("no_role", "user matched no role and ldap_default_role is empty")

    issuer = cfg.issuer
    subject = identity.subject

    row = (
        await session.execute(
            select(User, Principal)
            .join(Principal, Principal.id == User.principal_id)
            .where(
                User.auth_provider == "ldap",
                User.external_issuer == issuer,
                User.external_subject == subject,
            )
        )
    ).first()

    if row is not None:
        user, principal = row
    else:
        if not cfg.auto_provision:
            raise LDAPError("no_account", "no linked account and auto-provision is off")
        base = identity.username or (identity.email or "").split("@")[0] or subject
        username = await unique_username(session, base)
        principal = Principal(kind="user", global_role=role)
        session.add(principal)
        await session.flush()
        user = User(
            principal_id=principal.id,
            username=username,
            email=identity.email or None,
            password_hash=None,  # SSO-only: local password login is refused
            auth_provider="ldap",
            external_issuer=issuer,
            external_subject=subject,
        )
        session.add(user)
        await session.flush()

    if principal.disabled_at is not None:
        raise LDAPError("disabled", "account is disabled")

    role_changed = principal.global_role != role
    if role_changed:
        principal.global_role = role
    user.last_login_at = datetime.now(UTC)

    groups_changed = False
    if cfg.group_sync:
        groups_changed = await sync_external_groups(
            session, principal.id, identity.group_names, source="ldap"
        )
    return ProvisionResult(
        principal_id=str(principal.id),
        role_changed=role_changed,
        groups_changed=groups_changed,
    )


async def authenticate_ldap(
    session: AsyncSession, username: str, password: str, *, connector=connect
) -> ProvisionResult | None:
    """Full LDAP login (P6-T6): bind + resolve (blocking, threadpooled) then JIT
    provision. Returns a :class:`ProvisionResult` on success, ``None`` on an auth
    failure. Raises :class:`LDAPError` on config/transport/role-refusal errors —
    the login endpoint maps every one to a generic 401."""
    from starlette.concurrency import run_in_threadpool

    cfg = LdapConfig.from_settings()
    identity = await run_in_threadpool(
        resolve_ldap_identity, cfg, username, password, connector=connector
    )
    if identity is None:
        return None
    return await provision_ldap_principal(session, cfg, identity)
