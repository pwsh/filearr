"""Channel dispatch drivers (Phase 8, P8-T2 + the P8-T4 crypto slice).

The pure, network-free cores — rule matching, windowing, SSRF classification,
HMAC signing — live in the sibling modules and are implemented + tested. This
module wires the async I/O: the ``webhook`` and ``email`` drivers (in-tree core,
brief §1.5), plus the channel-secret encryption helpers (delegating to
:mod:`filearr.alerts.crypto`). ``apprise`` (R2) remains an optional-extra stub
(P8-T3).

Security posture (priority: security > integrity > reliability):

* **SSRF default-deny.** ``send_webhook`` resolves the target with a REAL
  ``socket.getaddrinfo`` resolver and vets EVERY A/AAAA record via
  :func:`filearr.alerts.ssrf.check_webhook_url` before connecting; a name host is
  then **pinned** to the validated IP for the actual socket, closing the
  DNS-rebinding TOCTOU. Redirects are never followed (a 3xx to a private IP is a
  classic SSRF-bypass vector). ``FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS`` (R5) is the
  only widening, and it permits the ``private`` class only.
* **Signed payloads.** Every webhook body carries ``X-Filearr-Signature`` (HMAC
  via :func:`filearr.alerts.signing.sign_payload`) so the receiver can verify us.
* **Bounded I/O.** Per-request timeout + response-size cap.
* **Secrets never in the clear.** Channel secrets are AES-GCM ciphertext at rest;
  decrypted values are used in-process only and never logged.

``dispatch_locality`` (R6) is authoritative per channel; this driver layer does
not auto-detect reachability or dual-dispatch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from filearr.alerts import crypto, signing, ssrf, webhook_formats
from filearr.errors import sanitize_error


@dataclass(frozen=True)
class RenderedAlert:
    """A rule match rendered once, then handed to each channel driver (§8.4).

    Built with the template-injection defenses from brief §5.3 (see
    :mod:`filearr.alerts.render`): file paths / filenames pass through
    ``errors.sanitize_error`` and are only ever template **variables**; the
    webhook ``payload`` is a plain dict serialized with ``json.dumps`` (never
    string-templated).
    """

    subject: str
    body_text: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Normalized outcome of one channel send (Apprise's per-service result and
    the core drivers' results collapse into this so retry/backoff is uniform)."""

    ok: bool
    detail: str = ""
    status_code: int | None = None
    retryable: bool = False


class ChannelDeliveryError(Exception):
    """A delivery failure raised by a driver so the caller (P8-T6 dispatch
    worker) can retry or give up. ``retryable`` distinguishes a transient failure
    (connection refused / timeout / 5xx / SMTP 4xx) from a permanent rejection
    (SSRF block / refused redirect / 4xx / SMTP 5xx). ``detail`` is already
    sanitized + safe to store in ``alert_events.last_error``."""

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# webhook (P8-T2)                                                              #
# --------------------------------------------------------------------------- #

def _system_resolver(host: str) -> list[str]:
    """Resolve ``host`` to all its A/AAAA records (blocking; run in a thread).

    Returns the de-duplicated list of IP strings. An empty list on failure lets
    :func:`ssrf.check_webhook_url` reject with ``no-dns-records`` rather than
    raising here."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


async def _threaded_resolver(host: str) -> list[str]:
    return await asyncio.to_thread(_system_resolver, host)


def _first_allowed_ip(
    resolved: tuple[tuple[str, ssrf.IpClass], ...], allow_private: bool
) -> str | None:
    ok = ssrf._PRIVATE_OK if allow_private else ssrf._PUBLIC_OK
    for ip, cls in resolved:
        if cls in ok:
            return ip
    return None


def _pin_hostname(url: str, ip: str) -> str:
    """Rewrite ``url``'s hostname to ``ip`` (preserving scheme/port/path/query),
    bracketing IPv6. The original host travels in the Host header + TLS SNI so
    the socket connects to the validated IP and cannot be re-resolved to a
    rebound private address between validation and connect."""
    parts = urlsplit(url)
    host_ip = f"[{ip}]" if ":" in ip else ip
    netloc = host_ip if parts.port is None else f"{host_ip}:{parts.port}"
    if parts.username:  # preserve any userinfo (unusual for webhooks, but safe)
        creds = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{creds}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def send_webhook(
    url: str,
    payload: dict,
    *,
    secret: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    max_response_bytes: int = 65536,
    allow_private: bool = False,
    resolver: ssrf.Resolver | None = None,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    now: int | None = None,
) -> DeliveryResult:
    """POST ``payload`` to ``url`` with HMAC signing + SSRF guard (brief §7.1).

    Raises :class:`ChannelDeliveryError` on any failure (SSRF block, refused
    redirect, >=400, network/timeout); returns a :class:`DeliveryResult` on 2xx.
    ``resolver`` / ``transport`` are injectable for tests; production uses the
    threaded ``getaddrinfo`` resolver and a real ``httpx`` transport with
    ``follow_redirects=False`` (always — enforced here, not left to the caller).
    """
    sync_resolver = resolver or _system_resolver
    verdict = ssrf.check_webhook_url(url, sync_resolver, allow_private=allow_private)
    if not verdict.allowed:
        # An SSRF-blocked target is a permanent, non-retryable rejection.
        raise ChannelDeliveryError(
            f"webhook target refused ({verdict.reason})",
            retryable=False,
        )

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode(
        "utf-8"
    )
    ts = now if now is not None else int(time.time())
    req_headers: dict[str, str] = {
        "content-type": "application/json",
        **{k.lower(): v for k, v in (headers or {}).items()},
    }
    if secret:
        req_headers["x-filearr-signature"] = signing.sign_payload(secret, body, ts)

    # Pin a name host to its validated IP for the actual connection (rebinding
    # defense). Only in the real path — an injected transport short-circuits DNS.
    target_url = url
    extensions: dict | None = None
    parts = urlsplit(url)
    host = parts.hostname or ""
    is_ip_literal = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip_literal = False
    if transport is None and not is_ip_literal:
        pinned = _first_allowed_ip(verdict.resolved, allow_private)
        if pinned is not None:
            target_url = _pin_hostname(url, pinned)
            req_headers["host"] = parts.netloc
            extensions = {"sni_hostname": host}

    client_kwargs: dict = {
        "follow_redirects": False,  # a redirect is an SSRF-bypass vector
        "timeout": httpx.Timeout(timeout_s),
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            request = client.build_request(
                "POST", target_url, content=body, headers=req_headers, extensions=extensions
            )
            resp = await client.send(request, stream=True)
            try:
                total = 0
                captured = bytearray()
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if len(captured) < max_response_bytes:
                        captured.extend(chunk[: max_response_bytes - len(captured)])
                    if total > max_response_bytes:
                        break
                status = resp.status_code
                detail_body = sanitize_error(bytes(captured).decode("utf-8", "replace"))
            finally:
                await resp.aclose()
    except httpx.TimeoutException as exc:
        raise ChannelDeliveryError(
            f"webhook timeout: {sanitize_error(exc)}", retryable=True
        ) from exc
    except httpx.TransportError as exc:
        raise ChannelDeliveryError(
            f"webhook transport error: {sanitize_error(exc)}", retryable=True
        ) from exc

    if 200 <= status < 300:
        return DeliveryResult(ok=True, status_code=status, detail=detail_body[:200])
    if 300 <= status < 400:
        raise ChannelDeliveryError(
            f"webhook redirect not followed (status {status})",
            retryable=False,
            status_code=status,
        )
    raise ChannelDeliveryError(
        f"webhook rejected (status {status}): {detail_body[:200]}",
        retryable=status >= 500,
        status_code=status,
    )



async def send_webhook_formatted(
    url: str,
    rendered: RenderedAlert,
    *,
    config: dict | None = None,
    secret: str | None = None,
    **send_kwargs,
) -> DeliveryResult:
    """FIX-16: POST a rendered alert to ``url`` in the channel's ``webhook_format``.

    ``generic`` (the back-compat default for any config lacking ``webhook_format``)
    sends ``rendered.payload`` byte-for-byte and keeps the HMAC signature; ``discord``
    / ``slack`` reshape the body (:mod:`filearr.alerts.webhook_formats`) so a foreign
    endpoint accepts it, and skip the signature (they never verify it). Every
    security control of :func:`send_webhook` (SSRF pinning, no-redirects, timeouts,
    size cap) is inherited unchanged via ``send_kwargs``."""
    fmt = webhook_formats.resolve_format(config)
    body = webhook_formats.format_body(
        fmt,
        subject=rendered.subject,
        body_text=rendered.body_text,
        payload=rendered.payload,
    )
    return await send_webhook(
        url,
        body,
        secret=webhook_formats.signing_secret(fmt, secret),
        **send_kwargs,
    )


# --------------------------------------------------------------------------- #
# email (P8-T2) — stdlib smtplib in a threadpool (no new dependency)          #
# --------------------------------------------------------------------------- #
# The P8 research recommended aiosmtplib; it is not a current dependency and the
# stdlib client covers STARTTLS/SSL/plain identically, so we use smtplib run off
# the event loop via asyncio.to_thread — zero new dependency surface (the
# security-first default). Swapping in aiosmtplib later keeps this signature.

_VALID_SECURITY = frozenset({"starttls", "ssl", "plain"})


def _send_email_sync(
    config: dict,
    rendered: RenderedAlert,
    timeout_s: float,
    attachment: tuple[str, bytes, str] | None = None,
) -> str:
    import smtplib
    from email.message import EmailMessage

    host = config.get("host")
    if not host:
        raise ChannelDeliveryError("email channel missing 'host'", retryable=False)
    port = int(config.get("port", 587))
    security = str(config.get("security", "starttls")).lower()
    if security not in _VALID_SECURITY:
        raise ChannelDeliveryError(
            f"email channel invalid security {security!r}", retryable=False
        )
    allow_insecure = bool(config.get("allow_insecure", False))
    if security == "plain" and not allow_insecure:
        # Refuse a plaintext downgrade unless explicitly opted out (brief §5.2).
        raise ChannelDeliveryError(
            "refusing plaintext SMTP (no STARTTLS/SSL); set allow_insecure to opt out",
            retryable=False,
        )
    sender = config.get("from_addr") or config.get("from")
    if not sender:
        raise ChannelDeliveryError("email channel missing 'from_addr'", retryable=False)
    recipients = config.get("to") or config.get("to_addrs") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r for r in recipients if r]
    if not recipients:
        raise ChannelDeliveryError("email channel has no recipients", retryable=False)
    username = config.get("username")
    password = config.get("password")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = rendered.subject
    msg.set_content(rendered.body_text)  # text/plain; already sanitized upstream
    if attachment is not None:
        # P11-T9 scheduled-report delivery: attach the generated artifact when it
        # is under the configured size cap. filename is sanitized upstream (a
        # content-addressed / report-derived name, never a raw user path).
        att_name, att_bytes, att_mime = attachment
        maintype, _, subtype = att_mime.partition("/")
        msg.add_attachment(
            att_bytes,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att_name,
        )

    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout_s)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout_s)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password or "")
            server.send_message(msg, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPResponseException as exc:
        # 4xx SMTP = transient (retry); 5xx = permanent.
        code = getattr(exc, "smtp_code", 500) or 500
        raise ChannelDeliveryError(
            f"smtp error {code}: {sanitize_error(getattr(exc, 'smtp_error', exc))}",
            retryable=400 <= code < 500,
            status_code=code,
        ) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise ChannelDeliveryError(
            f"smtp transport error: {sanitize_error(exc)}", retryable=True
        ) from exc
    return ", ".join(recipients)


async def send_email(
    config: dict,
    rendered: RenderedAlert,
    *,
    timeout_s: float = 30.0,
    attachment: tuple[str, bytes, str] | None = None,
) -> DeliveryResult:
    """Send ``rendered`` over SMTP (STARTTLS default; SSL/plain per config).

    Runs the blocking ``smtplib`` client off the event loop. Refuses a plaintext
    downgrade unless the channel explicitly opts out. Raises
    :class:`ChannelDeliveryError` (retryable-classified) on failure."""
    recipients = await asyncio.to_thread(
        _send_email_sync, config, rendered, timeout_s, attachment
    )
    return DeliveryResult(ok=True, detail=f"delivered to {recipients}")


# --------------------------------------------------------------------------- #
# apprise (P8-T3 — optional extra, still a stub)                              #
# --------------------------------------------------------------------------- #

async def send_via_apprise(apprise_url: str, rendered: RenderedAlert) -> DeliveryResult:
    """P8-T3: dispatch via the optional ``apprise`` extra, normalized to DeliveryResult.

    Errors with an actionable message ("install filearr[apprise]") when the
    optional dependency is absent but an ``apprise``-type channel is configured.
    """
    raise NotImplementedError("P8-T3: apprise adapter (optional filearr[apprise] extra)")


# --------------------------------------------------------------------------- #
# channel-secret encryption (P8-T4 slice — delegates to alerts.crypto)        #
# --------------------------------------------------------------------------- #

def encrypt_channel_secret(plaintext: str, key: bytes) -> str:
    """Envelope-encrypt a channel secret with AES-GCM (see :mod:`.crypto`).

    For an ``apprise``-type channel the encryption boundary is the **whole** URL
    string (it embeds tokens inline), not a sub-field (brief §7.2)."""
    return crypto.encrypt_secret(plaintext, key)


def decrypt_channel_secret(ciphertext: str, key: bytes) -> str:
    """Inverse of :func:`encrypt_channel_secret`; in-process at dispatch only.

    Decrypted values are never logged and never returned to the API client."""
    return crypto.decrypt_secret(ciphertext, key)
