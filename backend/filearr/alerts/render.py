"""Alert rendering (Phase 8, brief §5.3) — untrusted-path-safe.

A rule match is rendered ONCE into a :class:`~filearr.alerts.dispatch.RenderedAlert`
and handed to each channel driver. The load-bearing safety rule (brief §5.3): a
file path / filename is an UNTRUSTED, attacker-influenceable string (see
``errors.py``'s threat model), so it is

  * passed through :func:`filearr.errors.sanitize_error` (strip control chars,
    cap length) before it ever enters a rendered string, and
  * only ever a template **variable**, never in a template *source* position —
    this repo authors its own fixed templates (no user-authored templates in
    v1/v2), which closes the SSTI class outright.

Bodies here are ``text/plain`` (no HTML autoescape needed — there is no markup
context), and the webhook ``payload`` is a plain ``dict`` (the driver serializes
it with ``json.dumps``, never string-templating). If an HTML email body is added
later, render it through a ``select_autoescape(["html"])`` Jinja2 Environment.
"""

from __future__ import annotations

from filearr.alerts.dispatch import RenderedAlert
from filearr.errors import sanitize_error


def _clean(value: object) -> str:
    """Sanitize one untrusted scalar for inclusion in a rendered alert."""
    return sanitize_error(value)


def render_event(
    *,
    rule_name: str,
    event_type: str,
    rel_path: str | None = None,
    library_id: str | None = None,
    extra: dict | None = None,
) -> RenderedAlert:
    """Render a single file-event match to a plain-text + payload alert.

    Every free-form value is sanitized. ``rule_name`` is admin-authored but
    sanitized anyway (defense in depth). The webhook ``payload`` mirrors the body
    fields as structured data (no string templating on the JSON side)."""
    safe_rule = _clean(rule_name)
    safe_event = _clean(event_type)
    safe_path = _clean(rel_path) if rel_path is not None else None

    subject = f"[filearr] {safe_rule}: {safe_event}"
    lines = [f"Rule: {safe_rule}", f"Event: {safe_event}"]
    if safe_path is not None:
        lines.append(f"Path: {safe_path}")
    if library_id is not None:
        lines.append(f"Library: {_clean(library_id)}")
    body_text = "\n".join(lines) + "\n"

    payload: dict = {
        "rule": safe_rule,
        "event_type": safe_event,
    }
    if safe_path is not None:
        payload["rel_path"] = safe_path
    if library_id is not None:
        payload["library_id"] = _clean(library_id)
    if extra:
        # Sanitize both keys and scalar values one level deep; nested structures
        # are passed through json.dumps by the driver (still no string templating).
        payload["extra"] = {
            _clean(k): (_clean(v) if isinstance(v, str) else v)
            for k, v in extra.items()
        }
    return RenderedAlert(subject=subject, body_text=body_text, payload=payload)


def render_group(
    *,
    rule_name: str,
    event_type: str,
    library_id: str | None,
    events: list[dict],
    max_events: int,
    digest_window: str | None = None,
) -> RenderedAlert:
    """Render a grouped / digest alert covering many matches under one dedup key.

    ``events`` is the list of per-event payloads accumulated for the group (each a
    plain dict carrying at least ``rel_path``); they share ``event_type`` +
    ``library_id`` + rule (the fixed R1 group vocabulary), so those are rendered
    once in the header rather than per line. At most ``max_events`` paths are
    enumerated; the remainder collapse into an "and N more" tail so a pathological
    glob cannot inflate one notification into a megabyte body (P8-T7/T8). Every
    path is sanitized (untrusted filename, brief §5.3) and only ever a template
    variable.
    """
    safe_rule = _clean(rule_name)
    safe_event = _clean(event_type)
    total = len(events)
    kind = "digest" if digest_window else "alert"
    win = f" ({_clean(digest_window)} digest)" if digest_window else ""
    subject = f"[filearr] {safe_rule}: {total} {safe_event} {kind}{win}"

    lines = [f"Rule: {safe_rule}", f"Event: {safe_event}", f"Matches: {total}"]
    if library_id is not None:
        lines.append(f"Library: {_clean(library_id)}")
    lines.append("")

    shown = events[:max_events]
    paths: list[str] = []
    for ev in shown:
        # Prefer a file path; fall back to an ops-alert ``message`` (scan-failure /
        # extract-spike carry no rel_path) before stringifying the whole payload.
        rel = None
        if isinstance(ev, dict):
            rel = ev.get("rel_path") or ev.get("message")
        line = _clean(rel) if rel is not None else _clean(ev)
        lines.append(f"  - {line}")
        paths.append(line)
    remaining = total - len(shown)
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")
    body_text = "\n".join(lines) + "\n"

    payload: dict = {
        "rule": safe_rule,
        "event_type": safe_event,
        "count": total,
        "paths": paths,
    }
    if remaining > 0:
        payload["truncated"] = remaining
    if library_id is not None:
        payload["library_id"] = _clean(library_id)
    if digest_window:
        payload["digest_window"] = _clean(digest_window)
    return RenderedAlert(subject=subject, body_text=body_text, payload=payload)


def render_test(channel_name: str) -> RenderedAlert:
    """A sample alert for the per-channel test-fire endpoint (P8-T2/T12)."""
    safe = _clean(channel_name)
    return RenderedAlert(
        subject=f"[filearr] test alert for {safe}",
        body_text=(
            f"This is a filearr test notification for channel {safe!r}.\n"
            "If you received this, the channel is configured correctly.\n"
        ),
        payload={"test": True, "channel": safe},
    )
