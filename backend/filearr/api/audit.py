"""Security audit feed (Phase 6, P6-T9). ``GET /api/v1/audit`` — admin only.

Keyset-paginated over ``(ts DESC, id DESC)`` (a stable, index-backed cursor that
never skips or repeats rows as new events arrive), with optional filters on
event type, principal, and a time range."""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import SecurityEvent
from filearr.security import require_scope

router = APIRouter()


class SecurityEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    principal_id: uuid.UUID | None
    username_attempted: str | None
    ip: str | None
    user_agent: str | None
    ts: datetime
    details: dict | None


class AuditPage(BaseModel):
    events: list[SecurityEventOut]
    next_cursor: str | None


def _encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    raw = f"{ts.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from exc


def _out(row: SecurityEvent) -> SecurityEventOut:
    return SecurityEventOut(
        id=row.id,
        event_type=row.event_type,
        principal_id=row.principal_id,
        username_attempted=row.username_attempted,
        ip=str(row.ip) if row.ip is not None else None,
        user_agent=row.user_agent,
        ts=row.ts,
        details=row.details,
    )


@router.get(
    "/audit",
    response_model=AuditPage,
    dependencies=[Depends(require_scope("admin"))],
)
async def list_security_events(
    session: AsyncSession = Depends(get_session),
    event_type: str | None = Query(default=None),
    principal_id: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None, description="events at/after this ts"),
    until: datetime | None = Query(default=None, description="events at/before this ts"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AuditPage:
    conds = []
    if event_type:
        conds.append(SecurityEvent.event_type == event_type)
    if principal_id:
        conds.append(SecurityEvent.principal_id == principal_id)
    if since:
        conds.append(SecurityEvent.ts >= since)
    if until:
        conds.append(SecurityEvent.ts <= until)
    if cursor:
        c_ts, c_id = _decode_cursor(cursor)
        conds.append(
            or_(
                SecurityEvent.ts < c_ts,
                and_(SecurityEvent.ts == c_ts, SecurityEvent.id < c_id),
            )
        )
    stmt = select(SecurityEvent)
    if conds:
        stmt = stmt.where(and_(*conds))
    stmt = stmt.order_by(SecurityEvent.ts.desc(), SecurityEvent.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last.ts, last.id)
    return AuditPage(events=[_out(r) for r in rows], next_cursor=next_cursor)
