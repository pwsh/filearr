"""Semantic-search observability snapshot for ``/stats`` (P3-T8).

Cheap grouped counts over Postgres truth (``items.metadata`` JSONB): how many
active items carry a CURRENT-fingerprint vector, how many are still pending, and
how many carry a DRIFTED (old-model) vector that ``build_doc`` is omitting from
the projection. Read-only; degrades to zeros when semantic search is disabled."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.embed import FINGERPRINT_KEY, embedder_fingerprint
from filearr.models import Item, ItemStatus


async def semantic_snapshot(session: AsyncSession) -> dict:
    """Return ``{enabled, model, embedded_count, pending, fp_mismatches}``.

    * ``embedded_count`` — active items whose stored fingerprint matches the
      configured embedder (vector is live in the projection).
    * ``pending`` — active items with NO embedding fingerprint yet (never embedded
      / awaiting backfill).
    * ``fp_mismatches`` — active items whose stored fingerprint DIFFERS from the
      configured one (model changed → drift; vector omitted until re-embedded).

    When semantic search is disabled everything is zero (no scan is done)."""
    s = get_settings()
    if not s.semantic_enabled:
        return {
            "enabled": False,
            "model": s.embed_model,
            "embedded_count": 0,
            "pending": 0,
            "fp_mismatches": 0,
        }

    fp = embedder_fingerprint(s.embedder_config)
    has_fp = Item.metadata_.has_key(FINGERPRINT_KEY)
    fp_col = Item.metadata_[FINGERPRINT_KEY].astext
    row = (
        await session.execute(
            select(
                func.count().filter(has_fp & (fp_col == fp)),
                func.count().filter(~has_fp),
                func.count().filter(has_fp & (fp_col != fp)),
            ).where(Item.status == ItemStatus.active)
        )
    ).one()
    embedded, pending, mismatches = (int(row[0]), int(row[1]), int(row[2]))
    return {
        "enabled": True,
        "model": s.embed_model,
        "embedded_count": embedded,
        "pending": pending,
        "fp_mismatches": mismatches,
    }
