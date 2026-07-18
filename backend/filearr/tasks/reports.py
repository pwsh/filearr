"""P11-T5/T9 — background report-export task + scheduled-delivery evaluation.

* :func:`run_report_export` — the Procrastinate entrypoint on a DEDICATED
  ``exports`` queue at a LOW priority (research §7: a large export never starves
  scan/extract). It just opens a session and delegates to
  :func:`filearr.exports.run_export` (the lifecycle/diskguard/delivery machinery).
* :func:`defer_export_job` — enqueue a queued export from OUTSIDE the worker (the
  API POST handler), mirroring ``worker.defer_scan``.
* :func:`evaluate_report_schedules` — the once-per-occurrence cron evaluation for
  ``report_schedules``, reusing the EXACT ``due_occurrence`` +
  ``last_cron_fired_at`` discipline as ``scan_cron`` (FIX-8/FIX-9): a due schedule
  creates a ``report_exports`` row (``triggered_by='schedule'``) and enqueues it;
  ``last_cron_fired_at`` is the idempotency key that prevents a double-fire across
  duplicate/late ticks. Called from the minutely tick in ``worker.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import procrastinate
from sqlalchemy import select

from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.models import ReportExport, ReportSchedule
from filearr.schedule import due_occurrence
from filearr.worker import proc_app


@proc_app.task(
    queue="exports",
    name="filearr.tasks.reports.run_report_export",
    priority=get_settings().exports_priority,  # low: never starve scan/extract
)
async def run_report_export(export_id: str) -> dict:
    """Procrastinate entrypoint: run one queued export to completion."""
    from filearr import exports

    async with SessionLocal() as session:
        return await exports.run_export(session, uuid.UUID(export_id), get_settings())


async def defer_export_job(export_id: str | uuid.UUID) -> int | None:
    """Enqueue a queued export on the dedicated ``exports`` queue (API path)."""
    settings = get_settings()

    def _deferrer():
        return proc_app.configure_task(
            "filearr.tasks.reports.run_report_export",
            queue="exports",
            priority=settings.exports_priority,
        )

    try:
        return await _deferrer().defer_async(export_id=str(export_id))
    except procrastinate.exceptions.AppNotOpen:
        async with proc_app.open_async():
            return await _deferrer().defer_async(export_id=str(export_id))


async def evaluate_report_schedules(tick: datetime) -> list[str]:
    """Fire every report schedule due at ``tick`` (once-per-occurrence). Returns
    the ids of the exports enqueued.

    Mirrors ``worker._defer_due_scans``: ``due_occurrence`` against the schedule's
    persisted ``last_cron_fired_at`` yields the latest un-consumed occurrence; we
    stamp it back BEFORE enqueueing (committed first) so the occurrence fires at
    most once even across duplicate/late ticks or a mid-run worker death."""
    cap = get_settings().scan_schedule_max_catchup_minutes
    enqueued: list[str] = []
    async with SessionLocal() as session:
        schedules = list(
            (
                await session.execute(
                    select(ReportSchedule).where(ReportSchedule.enabled.is_(True))
                )
            ).scalars()
        )
        for sched in schedules:
            occ = due_occurrence(
                sched.cron, tick, sched.last_cron_fired_at, max_catchup_minutes=cap
            )
            if occ is None:
                continue
            sched.last_cron_fired_at = occ  # consume in the enqueue commit
            export = ReportExport(
                report_definition_id=sched.report_definition_id,
                canned_report_key=sched.canned_report_key,
                schedule_id=sched.id,
                triggered_by="schedule",
                owner_principal=sched.owner_principal,
                format=sched.format,
                params=dict(sched.params or {}),
                status="queued",
                delivery_status="pending",
            )
            session.add(export)
            await session.commit()
            await defer_export_job(export.id)
            enqueued.append(str(export.id))
    return enqueued
