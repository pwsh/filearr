"""P4-T1 — read-only metadata-profiles catalogue endpoint.

Exposes the code-shipped, per-``MediaType`` profile schemas (seeded into the
``metadata_profiles`` table at startup from :data:`filearr.profiles.METADATA_PROFILES`)
so the Admin UI / API consumers can list which well-known fields each media type
produces, with their type + faceting/sorting/label hints. Read scope — the
catalogue is not secret but stays behind the same Bearer gate as the rest of the
read API. Profiles are code-owned: there is deliberately NO POST/PATCH/DELETE
(a field-shape change ships as a code edit + a ``version`` bump, R2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import MetadataProfile
from filearr.schemas import MetadataProfileOut
from filearr.security import require_scope

router = APIRouter()


@router.get(
    "",
    response_model=list[MetadataProfileOut],
    dependencies=[Depends(require_scope("read"))],
)
async def list_metadata_profiles(
    session: AsyncSession = Depends(get_session),
) -> list[MetadataProfile]:
    """All seeded metadata profiles (one per ``MediaType``), ordered by media
    type, each with its FieldSpec projection under ``fields``."""
    rows = (
        await session.execute(
            select(MetadataProfile).order_by(MetadataProfile.media_type)
        )
    ).scalars().all()
    return list(rows)


@router.get(
    "/{media_type}",
    response_model=MetadataProfileOut,
    dependencies=[Depends(require_scope("read"))],
)
async def get_metadata_profile(
    media_type: str, session: AsyncSession = Depends(get_session)
) -> MetadataProfile:
    """A single profile by ``MediaType`` value; 404 if unknown/unseeded."""
    row = (
        await session.execute(
            select(MetadataProfile).where(MetadataProfile.media_type == media_type)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "metadata profile not found")
    return row
