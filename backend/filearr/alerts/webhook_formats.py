"""Per-channel webhook payload formatting (FIX-16).

Filearr's native webhook body is a compact JSON object (the ``generic`` format —
what every existing channel has always received, HMAC-signed). Foreign webhook
endpoints, however, impose their OWN required body shape: a Discord webhook
rejects a body without ``content``/``embeds`` (``50006 "Cannot send an empty
message"``); Slack wants ``text``/``blocks``. This module converts a rendered
alert (subject / body_text / structured payload) into the shape a given endpoint
expects.

Design / security notes:

* **``generic`` is byte-for-byte unchanged** — :func:`format_body` returns the
  original ``payload`` object untouched, so an existing channel (and its golden
  signature) is unaffected. This is the back-compat default for any channel row
  whose config lacks the ``webhook_format`` key.
* **HMAC only for ``generic``.** Discord/Slack never consume the
  ``X-Filearr-Signature`` header, so the driver skips it for them
  (:func:`signing_secret`). SSRF pinning / no-redirects / timeouts are the
  driver's job and are untouched by format.
* **No markdown-injection surprises.** Free-form values are already
  control-char-sanitized upstream (``errors.sanitize_error`` / render.py); here
  we additionally neutralize each destination's *markup* metacharacters
  (Discord markdown backslash-escaping; Slack ``& < >`` entity-escaping) and
  enforce every documented length limit with ellipsis truncation.

This module is pure (no I/O, no dispatch import) so it unit-tests in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

GENERIC = "generic"
DISCORD = "discord"
SLACK = "slack"
WEBHOOK_FORMATS: frozenset[str] = frozenset({GENERIC, DISCORD, SLACK})

_ELLIPSIS = "…"

# Discord documented limits (per-message / per-embed).
_DISCORD_CONTENT_MAX = 2000
_DISCORD_TITLE_MAX = 256
_DISCORD_DESC_MAX = 4096
_DISCORD_FIELD_NAME_MAX = 256
_DISCORD_FIELD_VALUE_MAX = 1024
_DISCORD_MAX_FIELDS = 25

# Slack: a section block's text tops out at 3000 chars; the top-level ``text``
# field is much larger (~40k) but we keep it sane at the same cap.
_SLACK_TEXT_MAX = 3000

# Severity → Discord embed color (decimal int). Discord's own palette.
_DISCORD_COLORS: dict[str, int] = {
    "critical": 0xED4245,  # red
    "error": 0xED4245,
    "warning": 0xFAA61A,  # amber
    "success": 0x57F287,  # green
    "info": 0x5865F2,  # blurple
}

_ERROR_EVENTS = frozenset(
    {"scan_failed", "extract_error_spike", "report_delivery_failed"}
)

# Ordered (payload-key, human-label) pairs surfaced as embed/section fields.
_FIELD_SPECS: tuple[tuple[str, str], ...] = (
    ("event_type", "Event"),
    ("library_id", "Library"),
    ("count", "Matches"),
    ("rel_path", "Path"),
    ("path", "Path"),
    ("disk_status", "Status"),
    ("pct_free", "Free %"),
    ("row_count", "Rows"),
    ("format", "Format"),
    ("schedule", "Schedule"),
    ("report", "Report"),
    ("channel_name", "Channel"),
    ("error", "Error"),
    ("truncated", "Truncated"),
    ("download_url", "Download"),
)


def detect_format(url: str | None) -> str:
    """Auto-detect the webhook format from the target URL (UI default helper).

    ``discord.com`` / ``discordapp.com`` ``…/api/webhooks/…`` → ``discord``;
    ``hooks.slack.com`` → ``slack``; anything else → ``generic``. Never raises."""
    if not url:
        return GENERIC
    try:
        parts = urlsplit(url)
    except ValueError:
        return GENERIC
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    discord_hosts = {"discord.com", "discordapp.com"}
    if (host in discord_hosts or any(host.endswith("." + h) for h in discord_hosts)) and (
        "/api/webhooks" in path
    ):
        return DISCORD
    if host == "hooks.slack.com":
        return SLACK
    return GENERIC


def resolve_format(config: dict | None) -> str:
    """The channel's configured ``webhook_format`` (``generic`` back-compat default).

    Send-time resolution NEVER auto-detects from the URL — an existing channel row
    without the key gets ``generic`` exactly as before this fix. Auto-detect is a
    new-channel UI convenience that *stores* the key up front."""
    fmt = (config or {}).get("webhook_format")
    if isinstance(fmt, str) and fmt in WEBHOOK_FORMATS:
        return fmt
    return GENERIC


def signing_secret(fmt: str, secret: str | None) -> str | None:
    """The HMAC secret to actually sign with: only ``generic`` carries a signature
    (Discord/Slack don't verify ``X-Filearr-Signature`` and would just see noise)."""
    return secret if fmt == GENERIC else None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + _ELLIPSIS


def _escape_discord(text: str) -> str:
    """Backslash-escape Discord markdown metacharacters so an untrusted filename
    cannot inject bold/italic/spoiler/code formatting. Backslash is escaped first."""
    out = text.replace("\\", "\\\\")
    for ch in ("*", "_", "~", "`", "|", ">"):
        out = out.replace(ch, "\\" + ch)
    return out


def _escape_slack(text: str) -> str:
    """Slack mrkdwn requires ``&``, ``<`` and ``>`` HTML-escaped; this also
    neutralizes ``<url|text>`` link injection from untrusted values."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _severity(payload: dict) -> str:
    if not isinstance(payload, dict):
        return "info"
    sev = payload.get("severity")
    if isinstance(sev, str) and sev.lower() in _DISCORD_COLORS:
        return sev.lower()
    disk = payload.get("disk_status")
    if disk == "critical":
        return "error"
    if disk == "warn":
        return "warning"
    if disk == "recovered":
        return "success"
    if payload.get("event_type") in _ERROR_EVENTS:
        return "error"
    return "info"


def _title(subject: str, payload: dict) -> str:
    """A concise title for the embed/section header."""
    if isinstance(payload, dict):
        for key in ("rule", "rule_name", "schedule", "report"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        if payload.get("test"):
            chan = payload.get("channel")
            return f"filearr test alert{f': {chan}' if chan else ''}"
    # Fall back to the human subject, minus the fixed ``[filearr] `` prefix.
    text = subject or "filearr alert"
    prefix = "[filearr] "
    return text[len(prefix) :] if text.startswith(prefix) else text


def _fields_from_payload(payload: dict) -> list[tuple[str, str]]:
    """Extract (label, value) pairs for the known key data, de-duplicating labels
    (``rel_path`` / ``path`` both map to "Path")."""
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    for key, label in _FIELD_SPECS:
        if label in seen_labels:
            continue
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value) if value else ""
        text = str(value)
        if not text:
            continue
        out.append((label, text))
        seen_labels.add(label)
    return out


def _discord_body(subject: str, body_text: str, payload: dict) -> dict:
    severity = _severity(payload)
    color = _DISCORD_COLORS.get(severity, _DISCORD_COLORS["info"])
    title = _truncate(_escape_discord(_title(subject, payload)), _DISCORD_TITLE_MAX)
    description = _truncate(_escape_discord(body_text or subject or ""), _DISCORD_DESC_MAX)
    content = _truncate(_escape_discord(subject or _title(subject, payload)), _DISCORD_CONTENT_MAX)

    fields: list[dict] = []
    for label, value in _fields_from_payload(payload):
        if len(fields) >= _DISCORD_MAX_FIELDS:
            break
        # A URL value is left unescaped (backslashes would break the link).
        safe_value = value if _looks_like_url(value) else _escape_discord(value)
        fields.append(
            {
                "name": _truncate(_escape_discord(label), _DISCORD_FIELD_NAME_MAX),
                "value": _truncate(safe_value, _DISCORD_FIELD_VALUE_MAX),
                "inline": True,
            }
        )

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if fields:
        embed["fields"] = fields
    return {"content": content, "embeds": [embed]}


def _slack_body(subject: str, body_text: str, payload: dict) -> dict:
    title = _escape_slack(_title(subject, payload))
    body = _escape_slack(body_text or subject or "")
    text = _truncate(body, _SLACK_TEXT_MAX)
    section_text = _truncate(f"*{title}*\n{body}", _SLACK_TEXT_MAX)
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": section_text}}
        ],
    }


def format_body(fmt: str, *, subject: str, body_text: str, payload: dict) -> dict:
    """Return the JSON object to POST for ``fmt``.

    ``generic`` returns ``payload`` UNCHANGED (same object → byte-identical
    serialization + signature). ``discord`` / ``slack`` build a destination-shaped
    body from the rendered subject/body/payload with every limit enforced and
    markup metacharacters neutralized."""
    if fmt == DISCORD:
        return _discord_body(subject, body_text, payload)
    if fmt == SLACK:
        return _slack_body(subject, body_text, payload)
    return payload
