"""P8-T2: webhook + email channel driver I/O wiring.

Webhook: HMAC signature round-trip, SSRF deny (fake resolver), the R5 private
override, redirect-not-followed, and transient(5xx)/permanent(4xx) classification
— all via an injected httpx MockTransport + injected resolver (no network).
Email: STARTTLS + auth + recipients via a monkeypatched stdlib smtplib, and the
plaintext-downgrade refusal.
"""

from __future__ import annotations

import httpx
import pytest

from filearr.alerts.dispatch import (
    ChannelDeliveryError,
    RenderedAlert,
    send_email,
    send_webhook,
)
from filearr.alerts.signing import verify_signature


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _private_resolver(_host: str) -> list[str]:
    return ["10.0.0.1"]


# --------------------------------------------------------------------------- #
# webhook                                                                     #
# --------------------------------------------------------------------------- #

async def test_webhook_signs_and_succeeds():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        captured["method"] = request.method
        return httpx.Response(200, text="ok")

    result = await send_webhook(
        "https://hook.test/path",
        {"a": 1, "b": "x"},
        secret="topsecret",
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        now=1000,
    )
    assert result.ok and result.status_code == 200
    assert captured["method"] == "POST"
    header = captured["headers"]["x-filearr-signature"]
    assert verify_signature(
        "topsecret", captured["body"], header, now=1000, max_age_s=300
    )
    # A tampered timestamp fails verification (freshness bound).
    assert not verify_signature(
        "topsecret", captured["body"], header, now=999_999, max_age_s=300
    )


async def test_webhook_ssrf_private_refused():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    with pytest.raises(ChannelDeliveryError) as ei:
        await send_webhook(
            "https://internal.test/",
            {},
            resolver=_private_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert ei.value.retryable is False
    assert called["n"] == 0  # never dialed


async def test_webhook_allow_private_permits():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    result = await send_webhook(
        "https://ha.lan/hook",
        {},
        resolver=_private_resolver,
        allow_private=True,
        transport=httpx.MockTransport(handler),
    )
    assert result.ok


async def test_webhook_redirect_not_followed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"location": "http://10.0.0.1/"})

    with pytest.raises(ChannelDeliveryError) as ei:
        await send_webhook(
            "https://hook.test/",
            {},
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert calls["n"] == 1  # the 302 target was NOT dialed
    assert ei.value.retryable is False
    assert ei.value.status_code == 302


async def test_webhook_500_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ChannelDeliveryError) as ei:
        await send_webhook(
            "https://hook.test/",
            {},
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert ei.value.retryable is True
    assert ei.value.status_code == 500


async def test_webhook_400_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="nope")

    with pytest.raises(ChannelDeliveryError) as ei:
        await send_webhook(
            "https://hook.test/",
            {},
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert ei.value.retryable is False
    assert ei.value.status_code == 400


async def test_webhook_ip_literal_public_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    # An IP literal skips DNS; a public one is allowed with no resolver.
    result = await send_webhook(
        "https://93.184.216.34/hook",
        {},
        transport=httpx.MockTransport(handler),
    )
    assert result.ok


async def test_webhook_ip_literal_private_refused():
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_webhook(
            "http://127.0.0.1/hook",
            {},
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
    assert ei.value.retryable is False


async def test_webhook_bad_scheme_refused():
    with pytest.raises(ChannelDeliveryError):
        await send_webhook(
            "ftp://hook.test/",
            {},
            resolver=_public_resolver,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )


# --------------------------------------------------------------------------- #
# email                                                                       #
# --------------------------------------------------------------------------- #

class _FakeSMTP:
    def __init__(self, log, ssl=False):
        self._log = log
        self._log.append(("ssl_ctor" if ssl else "ctor",))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self._log.append(("ehlo",))

    def starttls(self):
        self._log.append(("starttls",))

    def login(self, user, password):
        self._log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self._log.append(("send", from_addr, tuple(to_addrs)))


def _patch_smtp(monkeypatch):
    import smtplib

    log: list = []
    monkeypatch.setattr(
        smtplib, "SMTP", lambda host, port, timeout=None: _FakeSMTP(log)
    )
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda host, port, timeout=None: _FakeSMTP(log, ssl=True)
    )
    return log


async def test_email_starttls_auth_recipients(monkeypatch):
    log = _patch_smtp(monkeypatch)
    config = {
        "host": "smtp.test",
        "port": 587,
        "security": "starttls",
        "username": "u",
        "password": "p",
        "from_addr": "alerts@filearr.test",
        "to": ["a@x.test", "b@y.test"],
    }
    result = await send_email(config, RenderedAlert("subj", "body"))
    assert result.ok
    assert ("starttls",) in log
    assert ("login", "u", "p") in log
    send = next(e for e in log if e[0] == "send")
    assert send[1] == "alerts@filearr.test"
    assert send[2] == ("a@x.test", "b@y.test")


async def test_email_ssl_mode_no_starttls(monkeypatch):
    log = _patch_smtp(monkeypatch)
    config = {
        "host": "smtp.test",
        "port": 465,
        "security": "ssl",
        "username": "u",
        "password": "p",
        "from_addr": "alerts@filearr.test",
        "to": "a@x.test",
    }
    result = await send_email(config, RenderedAlert("s", "b"))
    assert result.ok
    assert ("ssl_ctor",) in log
    assert ("starttls",) not in log
    assert ("login", "u", "p") in log


async def test_email_plaintext_refused_by_default(monkeypatch):
    _patch_smtp(monkeypatch)
    config = {
        "host": "smtp.test",
        "port": 25,
        "security": "plain",
        "from_addr": "a@b.test",
        "to": ["x@y.test"],
    }
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_email(config, RenderedAlert("s", "b"))
    assert ei.value.retryable is False


async def test_email_plaintext_opt_in(monkeypatch):
    log = _patch_smtp(monkeypatch)
    config = {
        "host": "smtp.test",
        "port": 25,
        "security": "plain",
        "allow_insecure": True,
        "from_addr": "a@b.test",
        "to": ["x@y.test"],
    }
    result = await send_email(config, RenderedAlert("s", "b"))
    assert result.ok
    assert ("starttls",) not in log


async def test_email_no_recipients_is_permanent(monkeypatch):
    _patch_smtp(monkeypatch)
    config = {"host": "smtp.test", "security": "starttls", "from_addr": "a@b.test", "to": []}
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_email(config, RenderedAlert("s", "b"))
    assert ei.value.retryable is False
