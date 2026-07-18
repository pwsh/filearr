"""P11-T9 — scheduled-report CRUD.

A ``report_schedules`` row pairs a report source (a canned registry id OR a custom
``report_definition_id``, XOR) with a cron expression, an export format, and a
Phase-8 :class:`~filearr.models.AlertChannel` to deliver through. The minutely
worker tick (:func:`filearr.tasks.reports.evaluate_report_schedules`) fires a
background export per un-consumed occurrence and delivers it on completion.

Cron is validated on write with the SAME ``cronsim`` validator as ``scan_cron``
(:func:`filearr.schedule.validate_cron`); the friendly-schedule UI component
(``ScheduleField``) produces the stored UTC cron string, so the whole
Off/Hourly/Daily/Weekly/Monthly/Advanced surface is reused unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import AlertChannel, ReportDefinition, ReportSchedule
from filearr.reports import get_report
from filearr.schedule import InvalidCronError, validate_cron
from filearr.security import require_scope

router = APIRouter()

_FMT_PATTERN = "^(csv|ndjson|xml|xlsx)$"


class ReportScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    canned_report_key: str | None = None
    report_definition_id: uuid.UUID | None = None
    params: dict = Field(default_factory=dict)
    format: str = Field(default="csv", pattern=_FMT_PATTERN)
    cron: str = Field(min_length=1, max_length=200)
    channel_id: uuid.UUID | None = None
    enabled: bool = True
    owner_principal: str | None = None


class ReportScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    params: dict | None = None
    format: str | None = Field(default=None, pattern=_FMT_PATTERN)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    channel_id: uuid.UUID | None = None
    enabled: bool | None = None


class ReportScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_principal: str | None
    canned_report_key: str | None
    report_definition_id: uuid.UUID | None
    params: dict
    format: str
    cron: str
    channel_id: uuid.UUID | None
    enabled: bool
    last_cron_fired_at: datetime | None
    created_at: datetime
    updated_at: datetime


async def _validate_source(
    session: AsyncSession,
    canned_report_key: str | None,
    report_definition_id: uuid.UUID | None,
) -> None:
    if (canned_report_key is None) == (report_definition_id is None):
        raise HTTPException(
            422, "exactly one of canned_report_key / report_definition_id is required"
        )
    if canned_report_key is not None and get_report(canned_report_key) is None:
        raise HTTPException(422, f"unknown canned report {canned_report_key!r}")
    if report_definition_id is not None:
        if await session.get(ReportDefinition, report_definition_id) is None:
            raise HTTPException(422, "report definition not found")


async def _validate_channel(session: AsyncSession, channel_id: uuid.UUID | None) -> None:
    if channel_id is not None and await session.get(AlertChannel, channel_id) is None:
        raise HTTPException(422, "alert channel not found")


def _validate_cron(cron: str) -> None:
    try:
        validate_cron(cron)
    except InvalidCronError as exc:
        raise HTTPException(422, f"invalid cron: {exc}") from None


@router.get(
    "", response_model=list[ReportScheduleOut], dependencies=[Depends(require_scope("read"))]
)
async def list_schedules(
    session: AsyncSession = Depends(get_session),
) -> list[ReportSchedule]:
    stmt = select(ReportSchedule).order_by(ReportSchedule.name)
    return list((await session.execute(stmt)).scalars().all())


@router.post(
    "",
    response_model=ReportScheduleOut,
    status_code=201,
    dependencies=[Depends(require_scope("write"))],
)
async def create_schedule(
    body: ReportScheduleIn, session: AsyncSession = Depends(get_session)
) -> ReportSchedule:
    await _validate_source(session, body.canned_report_key, body.report_definition_id)
    await _validate_channel(session, body.channel_id)
    _validate_cron(body.cron)
    row = ReportSchedule(
        name=body.name,
        owner_principal=body.owner_principal,
        canned_report_key=body.canned_report_key,
        report_definition_id=body.report_definition_id,
        params=body.params or {},
        format=body.format,
        cron=body.cron,
        channel_id=body.channel_id,
        enabled=body.enabled,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.patch(
    "/{schedule_id}",
    response_model=ReportScheduleOut,
    dependencies=[Depends(require_scope("write"))],
)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ReportScheduleUpdate,
    session: AsyncSession = Depends(get_session),
) -> ReportSchedule:
    row = await session.get(ReportSchedule, schedule_id)
    if row is None:
        raise HTTPException(404, "schedule not found")
    fields = body.model_dump(exclude_unset=True)
    if "cron" in fields and fields["cron"] is not None:
        _validate_cron(fields["cron"])
    if "channel_id" in fields:
        await _validate_channel(session, fields["channel_id"])
    for k, v in fields.items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/{schedule_id}", status_code=204, dependencies=[Depends(require_scope("write"))]
)
async def delete_schedule(
    schedule_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    result = await session.execute(
        sa_delete(ReportSchedule).where(ReportSchedule.id == schedule_id)
    )
    if result.rowcount == 0:
        raise HTTPException(404, "schedule not found")
    await session.commit()
