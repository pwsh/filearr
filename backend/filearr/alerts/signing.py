"""Outbound webhook HMAC signing + verification (Phase 8, brief §7.1 pt 5).

Inert scaffolding for Phase 8, stdlib-only (``hmac``/``hashlib`` — no new
dependency). Filearr signs every outbound webhook so the **receiver** can verify
authenticity, and this module is also the reference a receiver reimplements to
check us. The header binds a ``timestamp`` into the signed content so a captured
payload cannot be replayed indefinitely (the receiver enforces a freshness
window).

Header format (Stripe/Svix-style, one line):
``t=<unix_ts>,sha256=<hex>`` where ``<hex> = HMAC_SHA256(secret,
f"{timestamp}." + body)``.

Verification is constant-time (:func:`hmac.compare_digest`) and the replay-window
check (:func:`within_replay_window`) is a separate pure function so it can be
unit-tested on its own.
"""

from __future__ import annotations

import hashlib
import hmac

_SCHEME = "sha256"


def _to_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _signed_content(body: str | bytes, timestamp: int) -> bytes:
    """The exact bytes that get HMAC'd: ``f"{timestamp}." + body``."""
    return f"{timestamp}.".encode() + _to_bytes(body)


def _compute(secret: str | bytes, body: str | bytes, timestamp: int) -> str:
    return hmac.new(
        _to_bytes(secret), _signed_content(body, timestamp), hashlib.sha256
    ).hexdigest()


def sign_payload(secret: str | bytes, body: str | bytes, timestamp: int) -> str:
    """Return the ``X-Filearr-Signature`` header value for ``body`` at ``timestamp``."""
    return f"t={timestamp},{_SCHEME}={_compute(secret, body, timestamp)}"


def parse_signature_header(header: str) -> tuple[int, str] | None:
    """Parse ``t=<ts>,sha256=<hex>`` → ``(timestamp, hex)`` or ``None`` if malformed."""
    ts: int | None = None
    sig: str | None = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                ts = int(value)
            except ValueError:
                return None
        elif key == _SCHEME:
            sig = value
    if ts is None or not sig:
        return None
    return ts, sig


def within_replay_window(timestamp: int, now: int, max_age_s: int) -> bool:
    """Pure replay check: is ``timestamp`` within ``±max_age_s`` of ``now``?

    Symmetric tolerance also rejects timestamps implausibly far in the *future*
    (clock-skew abuse), not only stale ones.
    """
    return abs(now - timestamp) <= max_age_s


def verify_signature(
    secret: str | bytes,
    body: str | bytes,
    header: str,
    *,
    now: int,
    max_age_s: int,
) -> bool:
    """Verify a signature header against ``body``: signature match **and** fresh.

    Returns ``True`` only if the header parses, its timestamp is within the
    replay window, and the recomputed HMAC matches in constant time. Both checks
    are always evaluated (no short-circuit) before the boolean is returned, so a
    caller cannot use timing to distinguish "bad signature" from "stale".
    """
    parsed = parse_signature_header(header)
    if parsed is None:
        return False
    timestamp, provided = parsed
    fresh = within_replay_window(timestamp, now, max_age_s)
    expected = _compute(secret, body, timestamp)
    match = hmac.compare_digest(expected, provided)
    return fresh and match
