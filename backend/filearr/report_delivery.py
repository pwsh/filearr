"""P11-T9 — scheduled report delivery through Phase-8 alert channels.

When a :class:`~filearr.models.ReportExport` produced by a schedule completes,
this module hands the artifact to the schedule's :class:`~filearr.models.AlertChannel`
using the SAME Phase-8 drivers (:mod:`filearr.alerts.dispatch`) and their
retryable/non-retryable classification — no second delivery stack:

* **email** — ATTACHES the artifact when its size is ``<= FILEARR_REPORT_EMAIL_MAX_BYTES``;
  above the cap it falls back to a link-style message carrying a row-count summary
  and the download URL (research OQ2). The file is never inlined past the cap.
* **webhook** — a JSON summary (report, rows, bytes, format) **plus the download
  URL**; the file bytes are NEVER embedded in a webhook body (research §4.4/§7).

A delivery failure is surfaced as an **ops alert event**
(:func:`filearr.alerts.ops.emit_report_delivery_failure`) — the existing
operator-facing pattern — and recorded on the export
(``delivery_status='failed'`` + sanitized ``delivery_error``); it never fails the
export job itself (the artifact is already produced and downloadable). Because
the schedule fires once-per-occurrence (``last_cron_fired_at``, FIX-9), a failure
is not hot-retried forever — the next occurrence produces a fresh export.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from filearr.alerts.dispatch import (
    ChannelDeliveryError,
    RenderedAlert,
    send_email,
    send_via_apprise,
    send_webhook_formatted,
)
from filearr.errors import sanitize_error
from filearr.models import AlertChannel, ReportExport, ReportSchedule

log = logging.getLogger("filearr.report_delivery")


def download_url(export: ReportExport, settings) -> str:
    """Absolute (or site-relative) download URL for an export artifact. Used in
    the webhook payload + the email link-fallback — never a bookmarkable token
    URL (research §7): the endpoint re-checks auth + RBAC at fetch time."""
    path = f"/api/v1/exports/{export.id}/download"
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def _summary(export: ReportExport, schedule: ReportSchedule, settings) -> dict:
    """The machine-readable delivery summary (webhook payload / email context)."""
    return {
        "event_type": "scheduled_report",
        "schedule": schedule.name,
        "report": export.canned_report_key or str(export.report_definition_id),
        "format": export.format,
        "row_count": export.row_count,
        "file_size_bytes": export.file_size_bytes,
        "download_url": download_url(export, settings),
        "export_id": str(export.id),
    }


def _render(
    export: ReportExport,
    schedule: ReportSchedule,
    summary: dict,
    *,
    with_link: bool,
) -> RenderedAlert:
    subject = f"[filearr] scheduled report: {sanitize_error(schedule.name)}"
    lines = [
        f"Report: {sanitize_error(schedule.name)}",
        f"Rows: {export.row_count}",
        f"Size: {export.file_size_bytes} bytes",
        f"Format: {export.format}",
    ]
    if with_link:
        lines.append(f"Download: {summary['download_url']}")
    body = "\n".join(lines) + "\n"
    return RenderedAlert(subject=subject, body_text=body, payload=summary)


async def _channel(session: AsyncSession, schedule: ReportSchedule) -> AlertChannel | None:
    if schedule.channel_id is None:
        return None
    ch = await session.get(AlertChannel, schedule.channel_id)
    if ch is None or not ch.enabled:
        return None
    return ch


def _decrypt(channel: AlertChannel) -> dict:
    from filearr.tasks.alerts import _decrypt_config

    return _decrypt_config(channel)


async def deliver_scheduled_export(
    session: AsyncSession, export: ReportExport, settings
) -> str:
    """Deliver a completed scheduled ``export`` through its schedule's channel.

    Returns the resulting ``delivery_status`` (``delivered`` / ``failed`` /
    ``skipped``). Records the status + any sanitized error on the export row and,
    on failure, emits a ``report_delivery_failed`` ops alert."""
    schedule = await session.get(ReportSchedule, export.schedule_id)
    if schedule is None:
        export.delivery_status = "skipped"
        await session.commit()
        return "skipped"
    channel = await _channel(session, schedule)
    if channel is None:
        export.delivery_status = "skipped"
        export.delivery_error = "no reachable channel (missing/disabled)"
        await session.commit()
        return "skipped"

    summary = _summary(export, schedule, settings)
    try:
        cfg = _decrypt(channel)
        if channel.type_ == "email":
            await _deliver_email(export, schedule, summary, cfg, settings)
        elif channel.type_ == "webhook":
            await _deliver_webhook(export, schedule, summary, cfg, settings)
        elif channel.type_ == "apprise":
            rendered = _render(export, schedule, summary, with_link=True)
            await send_via_apprise(cfg.get("url", ""), rendered)
        else:
            raise ChannelDeliveryError(
                f"unknown channel type {channel.type_!r}", retryable=False
            )
    except Exception as exc:  # noqa: BLE001 — classify + surface as ops alert
        detail = (
            exc.detail
            if isinstance(exc, ChannelDeliveryError)
            else sanitize_error(exc)
        )
        export.delivery_status = "failed"
        export.delivery_error = detail
        await session.commit()
        try:
            from filearr.alerts.ops import emit_report_delivery_failure

            await emit_report_delivery_failure(
                session,
                schedule_name=schedule.name,
                export_id=str(export.id),
                channel_name=channel.name,
                error=detail,
            )
        except Exception:  # noqa: BLE001 — ops-alert failure must not raise here
            log.warning("report-delivery ops alert failed", exc_info=True)
        return "failed"

    export.delivery_status = "delivered"
    export.delivery_error = None
    await session.commit()
    return "delivered"


async def _deliver_email(export, schedule, summary, cfg, settings) -> None:
    """Attach the artifact when small enough; else a link-style summary."""
    size = export.file_size_bytes or 0
    path = export.artifact_path
    can_attach = (
        path is not None
        and os.path.exists(path)
        and size <= settings.report_email_max_bytes
    )
    if can_attach:
        from filearr.reports import FORMAT_CONTENT_TYPE

        with open(path, "rb") as fh:
            data = fh.read()
        name = os.path.basename(path)
        mime = FORMAT_CONTENT_TYPE.get(export.format, "application/octet-stream")
        rendered = _render(export, schedule, summary, with_link=False)
        await send_email(cfg, rendered, attachment=(name, data, mime))
    else:
        # Over the cap (or missing) → link-style message with the row-count summary.
        rendered = _render(export, schedule, summary, with_link=True)
        await send_email(cfg, rendered)


async def _deliver_webhook(export, schedule, summary, cfg, settings) -> None:
    url = cfg.get("url")
    if not url:
        raise ChannelDeliveryError("webhook channel missing 'url'", retryable=False)
    # FIX-16: honour the channel's webhook_format. ``generic`` still POSTs the
    # summary dict byte-for-byte (payload of the rendered alert IS the summary);
    # discord/slack reshape it (title=report name, row-count + download link).
    rendered = _render(export, schedule, summary, with_link=True)
    await send_webhook_formatted(
        url,
        rendered,
        config=cfg,
        secret=cfg.get("secret"),
        timeout_s=settings.alert_webhook_timeout_s,
        max_response_bytes=settings.alert_webhook_max_response_bytes,
        allow_private=settings.webhook_allow_private_cidrs,
    )
