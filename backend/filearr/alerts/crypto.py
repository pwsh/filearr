"""Channel-secret encryption (Phase 8, P8-T4 slice — landed early for P8-T2).

Application-level **envelope** encryption for the secret sub-fields of an
``alert_channels.config`` row (webhook HMAC secret, SMTP password, and — for an
apprise channel — the whole URL string). The threat model (brief §7.2) is a
stolen Postgres dump / DB-level access *separate* from application-level access:
pgcrypto does not help there because the key would live where the DB process can
reach it. So the content key is derived from ``FILEARR_SECRET_KEY``, an env var /
mounted secret held **outside** Postgres — a dump alone yields only ciphertext.

Primitive: AES-256-GCM (authenticated encryption) via ``cryptography``
(Apache-2.0/BSD, no AGPL friction). The 32-byte content key is ``sha256(secret_key)``.
Wire format is base64 of ``nonce (12 bytes) || ciphertext||tag`` — a fresh random
nonce per encryption. GCM's auth tag makes tampering detectable: a modified
ciphertext raises on decrypt rather than returning garbage.

Secrets are NEVER logged and NEVER returned to an API client (the API redacts
secret fields on read and honours an "unchanged" sentinel on edit).
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from filearr.config import get_settings

_NONCE_BYTES = 12  # 96-bit nonce, the GCM standard


class SecretKeyMissing(RuntimeError):
    """Raised when channel-secret crypto is requested but FILEARR_SECRET_KEY is
    unset. The API surfaces this as a 503 with actionable guidance — it is never
    a silent plaintext fallback."""


class SecretDecryptError(ValueError):
    """Raised when a stored ciphertext cannot be decrypted (wrong key, corruption
    or tampering — GCM auth-tag failure)."""


def derive_key(secret_key: str) -> bytes:
    """Derive the 32-byte AES-256 content key from the configured secret.

    ``sha256`` of the UTF-8 secret. The secret is expected to be high-entropy
    (generated with ``secrets.token_urlsafe``), so a slow password KDF buys
    nothing here — the same reasoning the API-key hashing uses (security.py)."""
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def get_content_key() -> bytes | None:
    """Return the derived content key, or ``None`` when FILEARR_SECRET_KEY is
    unset (callers then refuse the operation rather than store plaintext)."""
    secret = get_settings().secret_key
    if not secret:
        return None
    return derive_key(secret)


def require_content_key() -> bytes:
    """Like :func:`get_content_key` but raises :class:`SecretKeyMissing` when the
    key is absent — used on the API write/test paths that must encrypt/decrypt."""
    key = get_content_key()
    if key is None:
        raise SecretKeyMissing(
            "FILEARR_SECRET_KEY is not set; it is required to store or read "
            "encrypted alert-channel secrets. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` and "
            "set it in the environment (kept OUTSIDE Postgres)."
        )
    return key


def encrypt_secret(plaintext: str, key: bytes) -> str:
    """AES-GCM encrypt ``plaintext`` under ``key``; return base64(nonce||ct)."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_secret(token: str, key: bytes) -> str:
    """Inverse of :func:`encrypt_secret`. Raises :class:`SecretDecryptError` on a
    wrong key or any tampering (GCM auth-tag mismatch)."""
    try:
        raw = base64.b64decode(token, validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretDecryptError("ciphertext is not valid base64") from exc
    if len(raw) <= _NONCE_BYTES:
        raise SecretDecryptError("ciphertext too short")
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except InvalidTag as exc:
        raise SecretDecryptError("decryption failed (wrong key or tampering)") from exc
