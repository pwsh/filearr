"""Meilisearch projection sync — the index is disposable and rebuildable."""

from sqlalchemy import select

from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.models import Item, ItemStatus, Library
from filearr.retrying import MEILI_RETRY
from filearr.search import (
    build_doc,
    delete_docs,
    load_projection_defs,
    parent_scope_map,
    upsert_docs,
)
from filearr.worker import proc_app


async def _expose_gps_map(session, items: list[Item]) -> dict:
    """P3-T11: {library_id: expose_gps} for the batch's libraries. Drives the GPS
    default-hidden gate in ``build_doc`` (default false => GPS stripped)."""
    lib_ids = {i.library_id for i in items}
    if not lib_ids:
        return {}
    rows = (
        await session.execute(
            select(Library.id, Library.expose_gps).where(Library.id.in_(lib_ids))
        )
    ).all()
    return {lid: bool(eg) for lid, eg in rows}


@proc_app.task(
    queue="index",
    name="filearr.tasks.index_sync.sync_items",
    retry=MEILI_RETRY,
    priority=get_settings().index_priority,  # UI-T14 default lane
)
async def sync_items(item_ids: list[str]) -> None:
    async with SessionLocal() as session:
        items = (
            (await session.execute(select(Item).where(Item.id.in_(item_ids)))).scalars().all()
        )
        expose = await _expose_gps_map(session, items)
        # P6-T3: parents' path_scope so sidecars inherit their RBAC scope.
        pscope = await parent_scope_map(session, items)
    # P4-T6: project facetable/sortable custom fields (loaded once per batch).
    defs = await load_projection_defs()
    live = [
        build_doc(
            i,
            defs,
            expose_gps=expose.get(i.library_id, False),
            parent_path_scope=pscope.get(i.sidecar_of),
        )
        for i in items
        if i.status == ItemStatus.active
    ]
    gone = [str(i.id) for i in items if i.status != ItemStatus.active]
    if live:
        await upsert_docs(live)
    if gone:
        # P9-T3: delete by EXPLICIT document id, never delete_documents_by_filter.
        # `gone` is resolved from the very rows this task loaded; a filter (e.g.
        # `status != active`) is re-evaluated at RUN time, so a row that flipped
        # back to active between enqueue and execution would be wrongly purged —
        # a TOCTOU gap. Explicit ids delete exactly what was diffed, nothing else.
        await delete_docs(gone)


@proc_app.task(
    queue="index",
    name="filearr.tasks.index_sync.rebuild_index",
    retry=MEILI_RETRY,
    priority=get_settings().index_priority,  # UI-T14 default lane
)
async def rebuild_index() -> int:
    """Full re-projection from Postgres (safety net / disaster recovery / schema
    rollout) via a SHADOW index + atomic swap.

    Redesigned for P9-T5: instead of upserting directly into the live index (which
    exposes a half-rebuilt index to concurrent searches mid-rebuild), the whole
    projection is built into a throwaway shadow index and swapped in atomically —
    a search NEVER sees a partial index, and any failure before the swap leaves the
    live index completely untouched. The heavy lifting (create shadow → apply the
    shared settings → backfill from Postgres → wait → swap → drop old) lives in
    ``meili_ops.rebuild_via_swap`` so the Meili-client orchestration is unit-tested
    in one place. ``sync_items`` (incremental) is deliberately unchanged — swap is
    for full rebuilds / schema rollouts only. Returns docs indexed.

    Disk headroom: a rebuild holds BOTH the live and shadow copies on disk at once
    (~2x the index size) until the post-swap delete — keep the Meili volume sized
    for it (see the ops runbook / P9-T11)."""
    from filearr.meili_ops import rebuild_via_swap

    return await rebuild_via_swap()
