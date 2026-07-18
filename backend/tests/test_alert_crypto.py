"""P8-T4 slice: channel-secret AES-GCM envelope encryption.

Pure (no DB, no network). Round-trip, tamper-detection, wrong-key rejection, and
the FILEARR_SECRET_KEY gating that makes the API 503 rather than store plaintext.
"""

from __future__ import annotations

import base64

import pytest

from filearr.alerts import crypto
from filearr.alerts.dispatch import decrypt_channel_secret, encrypt_channel_secret
from filearr.config import get_settings


def test_encrypt_decrypt_round_trip():
    key = crypto.derive_key("unit-test-secret")
    token = crypto.encrypt_secret("hunter2", key)
    assert token != "hunter2"
    assert crypto.decrypt_secret(token, key) == "hunter2"


def test_nonce_is_random_so_ciphertexts_differ():
    key = crypto.derive_key("k")
    a = crypto.encrypt_secret("same", key)
    b = crypto.encrypt_secret("same", key)
    assert a != b
    assert crypto.decrypt_secret(a, key) == crypto.decrypt_secret(b, key) == "same"


def test_tamper_is_detected():
    key = crypto.derive_key("k")
    token = crypto.encrypt_secret("value", key)
    raw = bytearray(base64.b64decode(token))
    raw[-1] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt_secret(tampered, key)


def test_wrong_key_rejected():
    token = crypto.encrypt_secret("value", crypto.derive_key("right"))
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt_secret(token, crypto.derive_key("wrong"))


def test_non_base64_rejected():
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt_secret("not base64 !!!", crypto.derive_key("k"))


def test_dispatch_wrappers_delegate():
    key = crypto.derive_key("k")
    token = encrypt_channel_secret("s", key)
    assert decrypt_channel_secret(token, key) == "s"


def test_content_key_gating(monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", None)
    assert crypto.get_content_key() is None
    with pytest.raises(crypto.SecretKeyMissing):
        crypto.require_content_key()
    monkeypatch.setattr(get_settings(), "secret_key", "configured-secret")
    key = crypto.require_content_key()
    assert isinstance(key, bytes) and len(key) == 32
