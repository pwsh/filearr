"""P4-T1 — read-only metadata-profiles catalogue endpoint.

Exposes the code-shipped, per-``file_category`` profile schemas (seeded into the
``metadata_profiles`` table at startup from :data:`filearr.profiles.METADATA_PROFILES`;
W8-B re-keyed these off the removed ``MediaType``) so the Admin UI / API consumers
can list which well-known fields each taxonomy category produces, with their type +
faceting/sorting/label hints. Read scope — the catalogue is not secret but stays
behind the same Bearer gate as the rest of the read API. Profiles are code-owned:
there is deliberately NO POST/PATCH/DELETE (a field-shape change ships as a code
edit + a ``version`` bump, R2).
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
    """All seeded metadata profiles (one per ``file_category``), ordered by
    category, each with its FieldSpec projection under ``fields``."""
    rows = (
        await session.execute(
            select(MetadataProfile).order_by(MetadataProfile.file_category)
        )
    ).scalars().all()
    return list(rows)


@router.get(
    "/{file_category}",
    response_model=MetadataProfileOut,
    dependencies=[Depends(require_scope("read"))],
)
async def get_metadata_profile(
    file_category: str, session: AsyncSession = Depends(get_session)
) -> MetadataProfile:
    """A single profile by ``file_category`` key; 404 if unknown/unseeded."""
    row = (
        await session.execute(
            select(MetadataProfile).where(MetadataProfile.file_category == file_category)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "metadata profile not found")
    return row
