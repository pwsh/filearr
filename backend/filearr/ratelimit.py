"""Brute-force rate limiting + account lockout (Phase 6, P6-T8).

A Postgres-backed **fixed-window counter with a lock**, chosen over slowapi /
in-memory limiters because the state MUST survive a process restart and be shared
across every worker — and Filearr deliberately runs no Redis. Two INDEPENDENT
buckets are tracked for each credential check:

* **per-username** (the *submitted* username string, lower-cased) — catches a
  distributed brute force (many source IPs, one target account) that no per-IP
  counter would ever notice; and
* **per-source-IP** — catches a single host trying many usernames.

Either bucket crossing ``FILEARR_AUTH_RATELIMIT_MAX_ATTEMPTS`` failures inside the
window locks that bucket for ``FILEARR_AUTH_RATELIMIT_LOCK_SECONDS``. The lock is
checked (→ 429 + ``Retry-After``) BEFORE the slow argon2 verify runs, so a locked
credential path costs one indexed SELECT rather than a KDF.

Every mutation commits in its **own** ``SessionLocal`` transaction, independent of
the login endpoint's request session — so a failed login's endpoint-level
``rollback`` (which discards partial provisioning) never rolls back the failure
counter. On a SUCCESSFUL auth the username bucket is cleared immediately; the IP
bucket is left to decay on its own window (it may be shared by other users behind
a NAT, so a single success must not reset everyone's counter)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request

from filearr.config import get_settings


@dataclass(frozen=True, slots=True)
class _Bucket:
    kind: str  # 'username' | 'ip'
    key: str


def client_ip(request: Request, *, trust_forwarded: bool | None = None) -> str | None:
    """The caller's source IP. When ``FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR``
    is set (a trusted reverse proxy — the Caddy TLS sidecar — is in front), the
    LEFTMOST ``X-Forwarded-For`` entry is used; otherwise the direct socket peer.
    Never trust the header by default: a client could otherwise spoof it to dodge
    the per-IP bucket (the per-username bucket stays unspoofable regardless)."""
    settings = get_settings()
    trust = (
        settings.auth_ratelimit_trust_forwarded_for
        if trust_forwarded is None
        else trust_forwarded
    )
    if trust:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else None


def _buckets(username: str | None, ip: str | None) -> list[_Bucket]:
    out: list[_Bucket] = []
    if username and username.strip():
        out.append(_Bucket("username", username.strip().lower()))
    if ip:
        out.append(_Bucket("ip", ip))
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def check_locked(username: str | None, ip: str | None) -> int | None:
    """Return the ``Retry-After`` seconds if EITHER bucket is currently locked,
    else ``None``. Read-only, own transaction, runs before any argon2 verify."""
    settings = get_settings()
    if not settings.auth_ratelimit_enabled:
        return None
    buckets = _buckets(username, ip)
    if not buckets:
        return None
    from filearr import db
    from filearr.models import AuthRateLimit

    now = datetime.now(UTC)
    retry: int | None = None
    async with db.SessionLocal() as s:
        for b in buckets:
            row = await s.get(AuthRateLimit, (b.kind, b.key))
            if row is not None and row.locked_until is not None:
                lu = _aware(row.locked_until)
                if now < lu:
                    secs = max(1, math.ceil((lu - now).total_seconds()))
                    retry = max(retry or 0, secs)
    return retry


async def register_failure(username: str | None, ip: str | None) -> bool:
    """Record a failed credential check against BOTH buckets (own transaction).
    Returns ``True`` iff this failure tripped a NEW lock (so the caller can emit a
    single ``lockout`` audit event rather than one per subsequent blocked try)."""
    settings = get_settings()
    if not settings.auth_ratelimit_enabled:
        return False
    buckets = _buckets(username, ip)
    if not buckets:
        return False
    from filearr import db
    from filearr.models import AuthRateLimit

    now = datetime.now(UTC)
    window = timedelta(seconds=settings.auth_ratelimit_window_seconds)
    lock = timedelta(seconds=settings.auth_ratelimit_lock_seconds)
    newly_locked = False
    async with db.SessionLocal() as s:
        for b in buckets:
            row = await s.get(AuthRateLimit, (b.kind, b.key))
            if row is None:
                row = AuthRateLimit(
                    bucket_kind=b.kind,
                    bucket_key=b.key,
                    window_start=now,
                    attempts=1,
                    locked_until=None,
                )
                s.add(row)
                locked_active = False
            else:
                locked_active = (
                    row.locked_until is not None and now < _aware(row.locked_until)
                )
                window_elapsed = now - _aware(row.window_start) >= window
                if window_elapsed and not locked_active:
                    # Stale window (and no live lock) → start a fresh window.
                    row.window_start = now
                    row.attempts = 1
                    row.locked_until = None
                else:
                    row.attempts = (row.attempts or 0) + 1
            if not locked_active and row.attempts >= settings.auth_ratelimit_max_attempts:
                row.locked_until = now + lock
                newly_locked = True
        await s.commit()
    return newly_locked


async def clear_username(username: str | None) -> None:
    """Clear the USERNAME bucket after a successful auth (own transaction). The IP
    bucket is intentionally left to decay on its own window."""
    settings = get_settings()
    if not settings.auth_ratelimit_enabled or not username or not username.strip():
        return
    from sqlalchemy import delete

    from filearr import db
    from filearr.models import AuthRateLimit

    async with db.SessionLocal() as s:
        await s.execute(
            delete(AuthRateLimit).where(
                AuthRateLimit.bucket_kind == "username",
                AuthRateLimit.bucket_key == username.strip().lower(),
            )
        )
        await s.commit()
