"""FIX-16: per-channel webhook payload formats (generic / discord / slack).

Pure formatter unit tests (shape, truncation at each documented limit, severity
colors, markdown-safety), URL auto-detect mapping, back-compat (a config without
``webhook_format`` → generic, byte-identical body), and the HMAC-only-for-generic
signing rule. No network.
"""

from __future__ import annotations

import json

import httpx

from filearr.alerts import webhook_formats as wf
from filearr.alerts.dispatch import RenderedAlert, send_webhook_formatted
from filearr.alerts.signing import verify_signature


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


# --------------------------------------------------------------------------- #
# auto-detect                                                                 #
# --------------------------------------------------------------------------- #

def test_detect_discord():
    assert wf.detect_format(
        "https://discord.com/api/webhooks/123/abc"
    ) == wf.DISCORD
    assert wf.detect_format(
        "https://discordapp.com/api/webhooks/123/abc"
    ) == wf.DISCORD
    assert wf.detect_format(
        "https://ptb.discord.com/api/webhooks/1/x"
    ) == wf.DISCORD


def test_detect_slack():
    assert wf.detect_format("https://hooks.slack.com/services/T/B/x") == wf.SLACK


def test_detect_generic_fallback():
    assert wf.detect_format("https://example.com/hook") == wf.GENERIC
    # A Discord host WITHOUT the webhooks path is not a webhook endpoint.
    assert wf.detect_format("https://discord.com/channels/1") == wf.GENERIC
    # Slack non-hooks host.
    assert wf.detect_format("https://api.slack.com/x") == wf.GENERIC
    assert wf.detect_format("") == wf.GENERIC
    assert wf.detect_format(None) == wf.GENERIC
    assert wf.detect_format("::not a url::") == wf.GENERIC


# --------------------------------------------------------------------------- #
# resolve_format / back-compat                                                #
# --------------------------------------------------------------------------- #

def test_resolve_format_backcompat_default():
    # No key at all (an existing channel row) → generic. NEVER auto-detects.
    assert wf.resolve_format({}) == wf.GENERIC
    assert wf.resolve_format(None) == wf.GENERIC
    assert wf.resolve_format({"url": "https://discord.com/api/webhooks/1/x"}) == wf.GENERIC


def test_resolve_format_explicit():
    assert wf.resolve_format({"webhook_format": "discord"}) == wf.DISCORD
    assert wf.resolve_format({"webhook_format": "slack"}) == wf.SLACK
    assert wf.resolve_format({"webhook_format": "bogus"}) == wf.GENERIC


def test_generic_body_is_the_payload_unchanged():
    payload = {"rule": "R", "event_type": "created", "paths": ["a/b"]}
    out = wf.format_body(wf.GENERIC, subject="s", body_text="b", payload=payload)
    assert out is payload  # same object → byte-identical json.dumps + signature


def test_generic_golden_bytes():
    payload = {"b": "x", "a": 1, "count": 2}
    out = wf.format_body(wf.GENERIC, subject="s", body_text="b", payload=payload)
    # Same serialization the driver uses (sorted keys) as a hardcoded golden.
    assert (
        json.dumps(out, separators=(",", ":"), sort_keys=True, default=str)
        == '{"a":1,"b":"x","count":2}'
    )


# --------------------------------------------------------------------------- #
# discord formatter                                                           #
# --------------------------------------------------------------------------- #

def test_discord_shape_has_content_and_embed():
    payload = {
        "rule": "MyRule",
        "event_type": "modified",
        "count": 3,
        "library_id": "libA",
        "paths": ["a/b.mkv"],
    }
    out = wf.format_body(
        wf.DISCORD,
        subject="[filearr] MyRule: 3 modified alert",
        body_text="Rule: MyRule\nEvent: modified\nMatches: 3\n",
        payload=payload,
    )
    # Discord requires content OR embeds — we always send content, so a body is
    # never "empty" (the 50006 bug).
    assert out["content"]
    assert isinstance(out["embeds"], list) and len(out["embeds"]) == 1
    embed = out["embeds"][0]
    assert embed["title"] == "MyRule"
    assert "modified" in embed["description"]
    assert embed["color"] == wf._DISCORD_COLORS["info"]
    assert "timestamp" in embed
    labels = {f["name"] for f in embed["fields"]}
    assert {"Event", "Matches", "Library"} <= labels


def test_discord_severity_colors():
    err = wf.format_body(
        wf.DISCORD, subject="s", body_text="b",
        payload={"event_type": "scan_failed", "rule_name": "sys"},
    )
    assert err["embeds"][0]["color"] == wf._DISCORD_COLORS["error"]

    warn = wf.format_body(
        wf.DISCORD, subject="s", body_text="b",
        payload={"event_type": "disk_low_space", "disk_status": "warn"},
    )
    assert warn["embeds"][0]["color"] == wf._DISCORD_COLORS["warning"]

    recovered = wf.format_body(
        wf.DISCORD, subject="s", body_text="b",
        payload={"event_type": "disk_low_space", "disk_status": "recovered"},
    )
    assert recovered["embeds"][0]["color"] == wf._DISCORD_COLORS["success"]


def test_discord_truncation_limits():
    long_desc = "x" * 9000
    long_val = "y" * 5000
    payload = {"rule": "z" * 900, "event_type": "created", "path": long_val}
    out = wf.format_body(
        wf.DISCORD, subject="s" * 5000, body_text=long_desc, payload=payload
    )
    embed = out["embeds"][0]
    assert len(out["content"]) <= wf._DISCORD_CONTENT_MAX
    assert len(embed["title"]) <= wf._DISCORD_TITLE_MAX
    assert len(embed["description"]) <= wf._DISCORD_DESC_MAX
    for f in embed["fields"]:
        assert len(f["name"]) <= wf._DISCORD_FIELD_NAME_MAX
        assert len(f["value"]) <= wf._DISCORD_FIELD_VALUE_MAX
    assert out["content"].endswith("…")


def test_discord_max_25_fields():
    payload = {f"k{i}": i for i in range(60)}
    payload["event_type"] = "created"
    out = wf.format_body(wf.DISCORD, subject="s", body_text="b", payload=payload)
    assert len(out["embeds"][0].get("fields", [])) <= wf._DISCORD_MAX_FIELDS


def test_discord_markdown_escaped():
    # An untrusted filename with markdown metacharacters must not inject formatting.
    payload = {"rule": "R", "event_type": "created", "rel_path": "a/*_~`|evil.txt"}
    out = wf.format_body(
        wf.DISCORD, subject="s", body_text="Path: a/*_~`|evil.txt", payload=payload
    )
    path_field = next(f for f in out["embeds"][0]["fields"] if f["name"] == "Path")
    assert "\\*" in path_field["value"]
    assert "\\_" in path_field["value"]
    assert "\\`" in path_field["value"]
    # description escaped too
    assert "\\*" in out["embeds"][0]["description"]


def test_discord_url_field_not_escaped():
    payload = {
        "schedule": "Nightly",
        "event_type": "scheduled_report",
        "row_count": 42,
        "download_url": "https://filearr.example/api/v1/exports/1/download",
    }
    out = wf.format_body(
        wf.DISCORD, subject="[filearr] scheduled report: Nightly",
        body_text="Report: Nightly\nRows: 42\n", payload=payload,
    )
    assert out["embeds"][0]["title"] == "Nightly"
    dl = next(f for f in out["embeds"][0]["fields"] if f["name"] == "Download")
    assert dl["value"] == "https://filearr.example/api/v1/exports/1/download"
    rows = next(f for f in out["embeds"][0]["fields"] if f["name"] == "Rows")
    assert rows["value"] == "42"


def test_discord_test_fire_title():
    out = wf.format_body(
        wf.DISCORD, subject="[filearr] test alert for chan",
        body_text="This is a filearr test notification.\n",
        payload={"test": True, "channel": "chan"},
    )
    assert "test alert" in out["embeds"][0]["title"]
    assert out["content"]


# --------------------------------------------------------------------------- #
# slack formatter                                                             #
# --------------------------------------------------------------------------- #

def test_slack_shape_and_escape():
    payload = {"rule": "R&D", "event_type": "created", "rel_path": "<a>&<b>"}
    out = wf.format_body(
        wf.SLACK, subject="[filearr] R&D: created",
        body_text="Rule: R&D\nPath: <a>&<b>\n", payload=payload,
    )
    assert out["text"]
    assert "&amp;" in out["text"]
    assert "&lt;" in out["text"] and "&gt;" in out["text"]
    assert out["blocks"][0]["type"] == "section"
    assert out["blocks"][0]["text"]["type"] == "mrkdwn"
    assert "*R&amp;D*" in out["blocks"][0]["text"]["text"]


def test_slack_truncation():
    out = wf.format_body(
        wf.SLACK, subject="s", body_text="z" * 9000, payload={"event_type": "created"}
    )
    assert len(out["text"]) <= wf._SLACK_TEXT_MAX
    assert len(out["blocks"][0]["text"]["text"]) <= wf._SLACK_TEXT_MAX


# --------------------------------------------------------------------------- #
# signing_secret rule                                                         #
# --------------------------------------------------------------------------- #

def test_signing_secret_generic_only():
    assert wf.signing_secret(wf.GENERIC, "s") == "s"
    assert wf.signing_secret(wf.DISCORD, "s") is None
    assert wf.signing_secret(wf.SLACK, "s") is None


# --------------------------------------------------------------------------- #
# send_webhook_formatted end-to-end (mock transport) — HMAC only for generic  #
# --------------------------------------------------------------------------- #

async def test_send_formatted_generic_signs_and_sends_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, text="ok")

    rendered = RenderedAlert(
        subject="s", body_text="b", payload={"rule": "R", "n": 1}
    )
    result = await send_webhook_formatted(
        "https://hook.test/x",
        rendered,
        config={},  # no webhook_format → generic
        secret="topsecret",
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        now=1000,
    )
    assert result.ok
    # generic → body is the payload, signed
    assert json.loads(captured["body"]) == {"rule": "R", "n": 1}
    header = captured["headers"]["x-filearr-signature"]
    assert verify_signature("topsecret", captured["body"], header, now=1000, max_age_s=300)


async def test_send_formatted_discord_reshapes_and_skips_hmac():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(204)

    rendered = RenderedAlert(
        subject="[filearr] R: created",
        body_text="Rule: R\nEvent: created\n",
        payload={"rule": "R", "event_type": "created"},
    )
    result = await send_webhook_formatted(
        "https://discord.com/api/webhooks/1/x",
        rendered,
        config={"webhook_format": "discord"},
        secret="topsecret",
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        now=1000,
    )
    assert result.ok
    body = json.loads(captured["body"])
    assert "embeds" in body and body["content"]
    # discord skips the HMAC header even though a secret was configured
    assert "x-filearr-signature" not in captured["headers"]
