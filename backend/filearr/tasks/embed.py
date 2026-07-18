"""P3-T8 local embedding pipeline — deferred-after-commit embed stage + backfill.

Semantic search is GLOBALLY OFF by default (``FILEARR_SEMANTIC_ENABLED=false``);
these tasks are inert until an operator opts in, then:

* :func:`embed_item` — the per-item embed stage. Deferred by ``extract_item``
  AFTER its commit (invariant 5 — never race an uncommitted row) on the dedicated
  ``embed`` queue at the LOWEST priority (below extract), so slow local ONNX
  inference never starves per-file metadata extraction. It computes the item's
  dense vector and stores it in ``metadata_._embedding`` (+ ``_embedding_fp`` drift
  fingerprint) — the extracted-fact column (invariant 2: the embed stage is an
  extractor) — then re-syncs the item so ``build_doc`` attaches ``_vectors``.
* :func:`embed_missing` — the backfill. Defers ``embed_item`` for active items
  that lack a current-fingerprint vector, CAPPED per run (the rest ride the next
  invocation), so a first-enable over a 750k corpus never enqueues an unbounded
  burst. Triggered by ``POST /api/v1/system/embed-backfill`` (admin).

Storing the vector in Postgres (a deliberate, ruling-approved refinement of the
scaffold's "Meili-only vectors" note) means ``rebuild_index`` re-projects the
existing vectors WITHOUT re-embedding — the one-time initial embed (~5.5 h
background) is never repeated on a rebuild/settings migration.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select

from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.embed import (
    EMBEDDING_KEY,
    FINGERPRINT_KEY,
    embed_source_from_item,
    embed_text_for_item,
    embed_texts,
    embedder_fingerprint,
)
from filearr.models import Item, ItemStatus
from filearr.worker import proc_app

logger = logging.getLogger(__name__)


@proc_app.task(queue="embed", name="filearr.tasks.embed.embed_item", retry=2)
async def embed_item(item_id: str) -> bool:
    """Compute + store one item's embedding vector, then re-sync it (P3-T8).

    No-op (returns False) when semantic search is disabled or the row is gone /
    not active — so a stale queued job after a disable is harmless. On success the
    vector + fingerprint land in ``metadata_`` and the Meili projection is
    refreshed (``build_doc`` picks up ``_vectors`` on the next sync)."""
    settings = get_settings()
    if not settings.semantic_enabled:
        return False
    cfg = settings.embedder_config
    fp = embedder_fingerprint(cfg)
    async with SessionLocal() as session:
        item = (
            await session.execute(select(Item).where(Item.id == item_id))
        ).scalar_one_or_none()
        if item is None or item.status != ItemStatus.active:
            return False
        text = embed_text_for_item(embed_source_from_item(item))
        vec = embed_texts([text], cfg)[0]
        item.metadata_ = {**item.metadata_, EMBEDDING_KEY: vec, FINGERPRINT_KEY: fp}
        await session.commit()

    from filearr.tasks.index_sync import sync_items

    await sync_items.defer_async(item_ids=[item_id])
    return True


@proc_app.task(queue="embed", name="filearr.tasks.embed.embed_missing", retry=2)
async def embed_missing() -> int:
    """Backfill embeddings for active items lacking a CURRENT-fingerprint vector,
    capped at ``FILEARR_EMBED_BACKFILL_BATCH`` per run (P3-T8).

    "Missing" = the item has no ``_embedding_fp`` OR its stored fingerprint no
    longer matches the configured embedder (drift after a model change). Defers a
    low-priority ``embed_item`` per selected id and returns how many were deferred;
    the operator (or a future periodic tick) re-runs it until it returns 0."""
    settings = get_settings()
    if not settings.semantic_enabled:
        return 0
    fp = embedder_fingerprint(settings.embedder_config)
    cap = settings.embed_backfill_batch
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Item.id)
                    .where(
                        Item.status == ItemStatus.active,
                        or_(
                            ~Item.metadata_.has_key(FINGERPRINT_KEY),
                            Item.metadata_[FINGERPRINT_KEY].astext != fp,
                        ),
                    )
                    .order_by(Item.id)
                    .limit(cap)
                )
            )
            .scalars()
            .all()
        )
    deferrer = proc_app.configure_task(
        "filearr.tasks.embed.embed_item",
        queue=settings.queue_embed,
        priority=settings.embed_priority,
    )
    for iid in rows:
        await deferrer.defer_async(item_id=str(iid))
    if rows:
        logger.info("embed_missing: deferred %d embed_item job(s)", len(rows))
    return len(rows)
