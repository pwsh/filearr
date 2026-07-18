"""Security audit log (Phase 6, P6-T9).

An append-only ``security_events`` trail for auth-relevant actions: login /
logout, account lifecycle (create / disable / enable / role-change / delete),
grant & group changes, lockouts, session revocation, and — opt-in — reads.

Design rules (all load-bearing):

* **Own transaction.** Every write opens its OWN ``SessionLocal`` session and
  commits independently, so an audit write is never rolled back by (nor rolls
  back) the request that triggered it.
* **Never break auth.** ``emit`` catches and logs every exception and swallows
  it. A broken audit sink must never turn a good login into a 500.
* **Secret scrub.** ``details`` is passed through a defensive recursive scrubber
  that redacts any key whose name looks like a secret (password / token / key /
  secret / cookie / authorization) before it ever reaches the row — belt-and-
  braces so a careless caller can't leak a credential into the log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request

logger = logging.getLogger("filearr.audit")

# --- event-type vocabulary (stable strings; also the audit-feed filter values) -
LOGIN_SUCCESS = "login_success"
LDAP_LOGIN = "ldap_login"
OIDC_LOGIN = "oidc_login"
LOGIN_FAILURE = "login_failure"
LOCKOUT = "lockout"
LOGOUT = "logout"
BOOTSTRAP = "bootstrap"
PASSWORD_CHANGE = "password_change"
USER_CREATED = "user_created"
USER_DISABLED = "user_disabled"
USER_ENABLED = "user_enabled"
ROLE_CHANGED = "role_changed"
USER_DELETED = "user_deleted"
GRANT_CREATED = "grant_created"
GRANT_DELETED = "grant_deleted"
GROUP_CREATED = "group_created"
GROUP_DELETED = "group_deleted"
GROUP_MEMBERSHIP_CHANGED = "group_membership_changed"
SESSION_REVOKED = "session_revoked"
SEARCH = "search"
# P11-T10 report export artifact download (data-exfiltration-shaped; audited
# unconditionally, mirroring the transfer download carve-out).
REPORT_EXPORT_DOWNLOADED = "report_export_downloaded"
# P5-T1 distributed-agent enrollment (fleet trust root)
AGENT_TOKEN_MINTED = "agent_token_minted"
AGENT_TOKEN_REVOKED = "agent_token_revoked"
AGENT_REGISTERED = "agent_registered"
AGENT_CERT_BOUND = "agent_cert_bound"
# P5-T2 (central half): step-ca JWK one-time token minted at register / re-issue
# (records agent_id + jti only -- NEVER the token itself).
AGENT_CA_OTT_MINTED = "agent_ca_ott_minted"
AGENT_REVOKED = "agent_revoked"
# Hard delete (?purge=true): the cleanup path for failed-enrollment pending rows
# and data-free decommissions. Refused while libraries/items reference the agent.
AGENT_DELETED = "agent_deleted"
# P5-T5 full-manifest reconciliation sweep (records agent_id + the anti-join
# counters on a mismatch finish, or the in-sync status on a start-match).
AGENT_RECONCILED = "agent_reconciled"
# P5-T6 config/policy push: an operator writes a new policy version (records the
# scope + new version only, NEVER the policy body).
AGENT_POLICY_UPDATED = "agent_policy_updated"
# P10-T1 on-demand agent command primitive (admin enqueue/cancel; NOT per-poll)
AGENT_COMMAND_ENQUEUED = "agent_command_enqueued"
AGENT_COMMAND_CANCELLED = "agent_command_cancelled"
# P10-T9 verification data-access: a completed rehash_check reads the file's full
# CONTENT on the agent (records kind + item/agent + outcome incl. any hash-mismatch
# correction). Audited UNCONDITIONALLY (regardless of FILEARR_AUDIT_READS) like the
# transfer download — bytes read off another machine (R2). A stat_check is a
# metadata-only existence probe and is NOT audited here.
AGENT_VERIFY_COMPLETED = "agent_verify_completed"
# P10-T5 staging integrity verification: a staged file whose hash/size disagrees
# with the catalog (records expected vs computed; the bytes are never served).
AGENT_TRANSFER_VERIFY_FAILED = "agent_transfer_verify_failed"
# P10-T13 RBAC-gated transfer API. Retrieve initiation + the completed download
# are audited UNCONDITIONALLY (bytes leaving the system, R2 / brief §4), i.e.
# regardless of FILEARR_AUDIT_READS. Cancel records the operator teardown.
AGENT_TRANSFER_INITIATED = "agent_transfer_initiated"
AGENT_TRANSFER_DOWNLOADED = "agent_transfer_downloaded"
AGENT_TRANSFER_CANCELLED = "agent_transfer_cancelled"
AGENT_SHARE_MAP_CREATED = "agent_share_map_created"
AGENT_SHARE_MAP_UPDATED = "agent_share_map_updated"
AGENT_SHARE_MAP_DELETED = "agent_share_map_deleted"
# P5-T7 signed update manifest: an operator uploads a (canary) release or
# promotes canary->general (records version + stage only, never the artifacts).
AGENT_RELEASE_UPLOADED = "agent_release_uploaded"
AGENT_RELEASE_PROMOTED = "agent_release_promoted"
# FIX-15 operator repair action: force-clearing a stuck ScanRun terminal.
SCAN_FORCE_CLEARED = "scan_force_cleared"

_SECRET_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "private",
    "session_hash",
)

_UA_MAX = 512
_USERNAME_MAX = 256


def _scrub(value: Any) -> Any:
    """Recursively redact secret-looking keys from a details payload."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(s in kl for s in _SECRET_SUBSTRINGS):
                out[str(k)] = "__redacted__"
            else:
                out[str(k)] = _scrub(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def actor_id(request: Request | None) -> str | None:
    """The acting principal's uuid parsed from ``request.state.actor`` (set to
    ``principal:<uuid>`` by the auth gate), or ``None`` for an API-key / anon
    actor (whose identifier is recorded in ``details`` instead)."""
    if request is None:
        return None
    actor = getattr(request.state, "actor", None)
    if isinstance(actor, str) and actor.startswith("principal:"):
        return actor.split(":", 1)[1]
    return None


async def emit(
    event_type: str,
    *,
    request: Request | None = None,
    principal_id: str | uuid.UUID | None = None,
    username_attempted: str | None = None,
    details: dict | None = None,
) -> None:
    """Write one security event (own transaction; never raises)."""
    try:
        from filearr import db
        from filearr.models import SecurityEvent

        ip: str | None = None
        ua: str | None = None
        if request is not None:
            from filearr.ratelimit import client_ip

            ip = client_ip(request)
            ua = request.headers.get("user-agent")
            if ua and len(ua) > _UA_MAX:
                ua = ua[:_UA_MAX]

        pid: uuid.UUID | None = None
        if principal_id is not None:
            pid = (
                principal_id
                if isinstance(principal_id, uuid.UUID)
                else uuid.UUID(str(principal_id))
            )

        uname = username_attempted
        if uname is not None and len(uname) > _USERNAME_MAX:
            uname = uname[:_USERNAME_MAX]

        row = SecurityEvent(
            event_type=event_type,
            principal_id=pid,
            username_attempted=uname or None,
            ip=ip,
            user_agent=ua,
            details=_scrub(details) if details else None,
        )
        async with db.SessionLocal() as s:
            s.add(row)
            await s.commit()
    except Exception:  # noqa: BLE001 — audit must never break the auth path
        logger.warning("audit emit failed for event_type=%s", event_type, exc_info=True)
