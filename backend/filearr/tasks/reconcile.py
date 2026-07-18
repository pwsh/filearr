"""P9-T7 — Postgres↔Meilisearch reconciliation sweep.

The load-bearing safety net behind the (no-delivery-retry) Meili task webhooks:
a periodic pass that detects and repairs any divergence between the disposable
search projection and Postgres truth, so worst-case index staleness is bounded
even if every incremental ``index_sync`` update or webhook was lost.

**Invariant 1 (disposable index) is preserved and this sweep NEVER writes
Postgres** — it only reads Postgres and mutates Meilisearch to match. The
inclusion rule mirrors the projection exactly: ``search.build_doc`` /
``index_sync`` index precisely the ``Item.status == active`` rows (sidecars
included — they carry an ``is_sidecar`` flag and are filtered out at *query*
time, not excluded from the projection). So the set that *should* be indexed is
``{id : status == active}``; everything else in Meili is an orphan.

Two-stage, cheap-first (brief §2a — ``GET /stats`` is O(1)):

1. **Cheap compare.** Postgres active-item count vs Meili ``numberOfDocuments``.
   Within ``tolerance`` → no-op. Skipped entirely while the ``index`` queue has
   ``todo``/``doing`` work, because an in-flight backlog *legitimately* explains a
   transient count gap and would otherwise trigger a false-alarm full diff.
2. **Full id diff** (only when the cheap compare shows drift). Stream the active
   Postgres ids (server-side, ``yield_per`` chunked) and paginate every Meili
   document id (``fields=[id]``); upsert the ids missing from Meili (rebuilt from
   Postgres rows via the existing ``build_doc`` path) and delete the orphan ids
   present only in Meili (explicit-id delete — never delete-by-filter, invariant
   from P9-T3). Work is capped per sweep; the remainder is carried to the next.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, text

from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.models import Item, ItemStatus
from filearr.search import (
    build_doc,
    client,
    delete_docs,
    load_projection_defs,
    parent_scope_map,
    upsert_docs,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcilePlan:
    """The bounded repair plan produced from a Postgres/Meili id diff (pure)."""

    to_upsert: list[str]  # active in Postgres, absent from Meili → (re)index
    to_delete: list[str]  # present in Meili, not an active Postgres row → purge
    missing_total: int  # full missing count before the per-sweep cap
    orphan_total: int  # full orphan count before the per-sweep cap
    carried: int  # fixes deferred to the next sweep because of the cap

    @property
    def capped(self) -> bool:
        return self.carried > 0


def plan_reconcile(
    pg_ids: set[str], meili_ids: set[str], max_fixes: int
) -> ReconcilePlan:
    """Diff the two id sets into a capped repair plan (pure, no I/O).

    Orphan deletes are budgeted *before* missing upserts: an orphan is a doc that
    is actively served in search results for an item that should no longer appear
    (deleted / tombstoned), which is the more user-visible correctness defect, so
    it gets priority under a tight cap. Ids are sorted for deterministic,
    reproducible batches. ``max_fixes`` bounds the *total* mutations this sweep
    performs; the overflow is reported via ``carried`` for the next sweep.
    """
    missing = sorted(pg_ids - meili_ids)
    orphans = sorted(meili_ids - pg_ids)
    budget = max(max_fixes, 0)
    del_batch = orphans[:budget]
    budget -= len(del_batch)
    up_batch = missing[:budget] if budget > 0 else []
    carried = (len(orphans) - len(del_batch)) + (len(missing) - len(up_batch))
    return ReconcilePlan(
        to_upsert=up_batch,
        to_delete=del_batch,
        missing_total=len(missing),
        orphan_total=len(orphans),
        carried=carried,
    )


async def _index_queue_busy(session) -> bool:
    """True if the ``index`` queue has queued/in-flight jobs (avoid false alarms).

    Reads ``procrastinate_jobs`` directly (same cheap pattern as ``queue_stats``);
    a DB without the procrastinate schema is treated as not-busy so a fresh deploy
    can still reconcile."""
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return False
    n = (
        await session.execute(
            text(
                "SELECT count(*) FROM procrastinate_jobs "
                "WHERE queue_name = :q AND status IN ('todo', 'doing')"
            ),
            {"q": get_settings().queue_index},
        )
    ).scalar()
    return bool(n)


async def _postgres_active_count(session) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Item).where(Item.status == ItemStatus.active)
        )
    ).scalar_one()


async def _postgres_active_ids(session) -> set[str]:
    """Stream the active-item ids server-side (chunked) into a set of strings."""
    ids: set[str] = set()
    result = await session.stream_scalars(
        select(Item.id)
        .where(Item.status == ItemStatus.active)
        .execution_options(yield_per=get_settings().reconcile_pg_chunk)
    )
    async for row in result:
        ids.add(str(row))
    return ids


async def _meili_document_ids(index) -> set[str]:
    """Paginate every Meili document id (``fields=[id]`` keeps the payload tiny)."""
    page = get_settings().reconcile_meili_page
    ids: set[str] = set()
    offset = 0
    while True:
        info = await index.get_documents(offset=offset, limit=page, fields=["id"])
        results = info.results
        for doc in results:
            ids.add(str(doc["id"]))
        if len(results) < page:
            break
        offset += page
    return ids


async def _reindex_missing(ids: list[str]) -> int:
    """Rebuild docs for ``ids`` from Postgres truth and upsert them. Returns the
    number of documents actually upserted (an id whose row is no longer active is
    silently skipped — the next sweep will delete it as an orphan)."""
    if not ids:
        return 0
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(Item).where(Item.id.in_(ids)))).scalars().all()
        )
        # P6-T3: parents' path_scope so sidecars inherit their RBAC scope.
        pscope = await parent_scope_map(session, rows)
    # P4-T6: project facetable/sortable custom fields (loaded once per repair).
    defs = await load_projection_defs()
    docs = [
        build_doc(i, defs, parent_path_scope=pscope.get(i.sidecar_of))
        for i in rows
        if i.status == ItemStatus.active
    ]
    if docs:
        await upsert_docs(docs)
    return len(docs)


async def run_reconcile_sweep(
    *, max_fixes: int | None = None, tolerance: int | None = None
) -> dict:
    """Run one reconciliation sweep. Returns a structured outcome (also logged).

    NEVER writes Postgres. Reads Postgres, mutates only Meilisearch to converge on
    Postgres truth. Safe to run concurrently-guarded by the caller's
    ``queueing_lock`` (one sweep at a time)."""
    s = get_settings()
    max_fixes = s.reconcile_max_fixes if max_fixes is None else max_fixes
    tolerance = s.reconcile_tolerance if tolerance is None else tolerance

    async with SessionLocal() as session:
        if await _index_queue_busy(session):
            logger.info("reconcile: skipped — index queue has pending work")
            return {"status": "skipped", "reason": "index_queue_busy"}
        pg_count = await _postgres_active_count(session)

    async with client() as c:
        index = c.index(s.meili_index)
        meili_count = (await index.get_stats()).number_of_documents
        if abs(pg_count - meili_count) <= tolerance:
            logger.info(
                "reconcile: in sync (postgres=%d meili=%d tolerance=%d)",
                pg_count,
                meili_count,
                tolerance,
            )
            return {
                "status": "in_sync",
                "postgres": pg_count,
                "meili": meili_count,
            }
        logger.info(
            "reconcile: drift detected (postgres=%d meili=%d) — running full id diff",
            pg_count,
            meili_count,
        )
        meili_ids = await _meili_document_ids(index)

    async with SessionLocal() as session:
        pg_ids = await _postgres_active_ids(session)

    plan = plan_reconcile(pg_ids, meili_ids, max_fixes)
    if plan.to_delete:
        # Explicit-id delete only (P9-T3 TOCTOU guard: never delete-by-filter).
        await delete_docs(plan.to_delete)
    upserted = await _reindex_missing(plan.to_upsert)

    outcome = {
        "status": "repaired",
        "postgres": pg_count,
        "meili": meili_count,
        "missing_total": plan.missing_total,
        "orphan_total": plan.orphan_total,
        "upserted": upserted,
        "deleted": len(plan.to_delete),
        "carried": plan.carried,
        "capped": plan.capped,
    }
    logger.info(
        "reconcile: repaired — upserted=%d deleted=%d (missing_total=%d orphan_total=%d "
        "carried=%d capped=%s)",
        upserted,
        len(plan.to_delete),
        plan.missing_total,
        plan.orphan_total,
        plan.carried,
        plan.capped,
    )
    return outcome
