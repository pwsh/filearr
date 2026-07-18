"""Aggregate stats endpoints (P3-T14 timeline).

A thin, cheap grouped-count layer over Postgres truth — no new infra, no index
beyond the existing columns. The timeline is a date histogram over ``items.mtime``
(``date_trunc`` by month/year) that the frontend renders as clickable bars; a
click maps a bucket to an ``mtime`` range filter on ``/search``.

There is no dedicated ``mtime`` index today, so this is a bounded full-column
aggregate (grouped count over two columns). At homelab scale that is trivially
cheap; if a very large corpus ever makes it slow, add a b-tree on
``items(mtime)`` (noted as a future optimisation, not needed now)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import Item
from filearr.schemas import TimelineBucket, TimelineResponse
from filearr.security import PermissionContext, require_permission

router = APIRouter()

# FIX-3: an mtime more than 48h in the future is a SUSPECT timestamp (bad copy
# tool / mis-set clock), NOT a real point on the timeline — it is counted into a
# separate "invalid dates" bar rather than distorting the histogram's range. This
# matches ``search.recency_bucket``'s 48h future-skew window.
_FUTURE_SKEW = timedelta(hours=48)


def _next_boundary(start: datetime, bucket: str) -> datetime:
    """The exclusive upper edge of a month/year bucket whose lower edge is
    ``start`` (a ``date_trunc`` result). Pure calendar arithmetic — no dateutil."""
    if bucket == "year":
        return start.replace(year=start.year + 1)
    # month
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


@router.get(
    "/timeline",
    response_model=TimelineResponse,
)
async def timeline(
    library: uuid.UUID | None = Query(default=None, description="scope to one library"),
    bucket: str = Query(
        default="month",
        pattern="^(month|year)$",
        description="histogram granularity: month or year",
    ),
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> TimelineResponse:
    """Date histogram of active items by ``mtime`` (P3-T14).

    Buckets are ``date_trunc(bucket, mtime)`` counts over ``status='active'`` items
    (optionally scoped to ``library``), ascending. Items with an mtime beyond the
    48h future-skew window are excluded from the bars and reported as
    ``invalid_count`` with an ``invalid_mtime_gte`` the UI can turn into a
    ``mtime_gte`` filter to inspect them."""
    now = datetime.now(UTC)
    future_edge = now + _FUTURE_SKEW
    # +1s so the threshold expresses "strictly beyond the 48h window" as a
    # ``mtime_gte`` (build_filters uses ``mtime >= gte``).
    invalid_mtime_gte = int(future_edge.timestamp()) + 1

    # Truncate in UTC explicitly (``mtime AT TIME ZONE 'UTC'``) so buckets are
    # deterministic regardless of the DB session time zone; the result is a naive
    # UTC wall-clock timestamp we re-stamp as UTC below.
    bucket_col = func.date_trunc(
        bucket, Item.mtime.op("AT TIME ZONE")("UTC")
    ).label("bucket")
    base = Item.status == "active"
    if library is not None:
        base = base & (Item.library_id == library)
    # P6-T4: histogram counts only the caller's readable items.
    scope_clause = ctx.sql_clause()
    if scope_clause is not None:
        base = base & scope_clause

    rows = (
        await session.execute(
            select(bucket_col, func.count())
            .where(base & (Item.mtime <= future_edge))
            .group_by(bucket_col)
            .order_by(bucket_col)
        )
    ).all()

    buckets: list[TimelineBucket] = []
    for start, count in rows:
        if start is None:
            continue
        # date_trunc(... AT TIME ZONE 'UTC') returns a naive UTC timestamp.
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        end = _next_boundary(start, bucket)
        buckets.append(
            TimelineBucket(
                start=start,
                start_epoch=int(start.timestamp()),
                end_epoch=int(end.timestamp()),
                count=int(count),
            )
        )

    invalid_count = (
        await session.execute(
            select(func.count())
            .select_from(Item)
            .where(base & (Item.mtime > future_edge))
        )
    ).scalar_one()

    return TimelineResponse(
        bucket=bucket,
        library=library,
        buckets=buckets,
        invalid_count=int(invalid_count),
        invalid_mtime_gte=invalid_mtime_gte,
    )
