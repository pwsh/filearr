import logging
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import __version__
from filearr.config import get_settings
from filearr.db import get_session
from filearr.embed_stats import semantic_snapshot
from filearr.errors import (
    extract_error_counts_by_library,
    failed_jobs,
    failed_jobs_count,
)
from filearr.jobs_stats import jobs_summary, running_jobs, thumbnail_totals
from filearr.meili_stats import meili_snapshot
from filearr.models import Item, ItemStatus
from filearr.queue_stats import queue_snapshot
from filearr.schemas import FailedJobPage
from filearr.security import require_scope

router = APIRouter()
log = logging.getLogger("filearr.system")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/version")
async def version() -> dict:
    """Identify the RUNNING build. ``build_stamp`` is written into the image
    by the deploy script (backend/.build-stamp -> /app/.build-stamp) and is
    the ground truth for "which source is this container actually running" —
    the same stamp the deploy's verify step checks. Null outside deployed
    images (dev checkouts have no stamp)."""
    from starlette.concurrency import run_in_threadpool

    # ``source_url`` (AGPL-3.0 §13, FILEARR_SOURCE_URL) is served here so the
    # footer "Source" link can point at a fork's modified source at RUNTIME
    # without a frontend rebuild -- the Vite __SOURCE_URL__ is only the fallback.
    return {
        "app_version": __version__,
        "build_stamp": await run_in_threadpool(_read_stamp),
        "source_url": get_settings().source_url,
        # P5-T1: whether the distributed-agent fleet surface is enabled (drives
        # the Admin -> Agents panel's visibility). Opt-in, default off.
        "agents_enabled": get_settings().agents_enabled,
    }


def _read_stamp() -> str | None:
    import pathlib as _pl

    candidates = (
        _pl.Path("/app/.build-stamp"),
        _pl.Path(__file__).resolve().parents[2] / ".build-stamp",
    )
    for cand in candidates:
        try:
            return cand.read_text().strip() or None
        except OSError:
            continue
    return None


async def _thumbnail_stats(session: AsyncSession) -> dict:
    """Cheap thumbnail-cache aggregates from ``thumbnail_manifest`` (P12-T12).

    ``count`` / ``bytes`` are the whole-cache totals; ``by_source`` breaks them
    down by generator source (artwork / image / audio_embedded / video) so an
    operator can see, e.g., how much of the store is video poster-frames. The
    grouped aggregate itself comes from :func:`filearr.jobs_stats.thumbnail_totals`
    (single source of truth, shared with the Jobs ``thumbs`` monitor); this layer
    adds the soft-budget check. A WARNING is logged (never blocks) when the total
    exceeds the configured ``thumbnail_total_budget_bytes`` budget."""
    totals = await thumbnail_totals(session)
    total_count = totals["count"]
    total_bytes = totals["bytes"]

    budget = get_settings().thumbnail_total_budget_bytes
    over_budget = budget > 0 and total_bytes > budget
    if over_budget:
        log.warning(
            "thumbnail cache %s bytes exceeds soft budget %s bytes",
            total_bytes,
            budget,
        )
    return {
        "count": total_count,
        "bytes": total_bytes,
        "by_source": totals["by_source"],
        "budget_bytes": budget,
        "over_budget": over_budget,
    }


@router.get("/stats", dependencies=[Depends(require_scope("read"))])
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    rows = await session.execute(
        select(Item.file_category, func.count(), func.coalesce(func.sum(Item.size), 0))
        .where(Item.status == "active")
        .group_by(Item.file_category)
    )
    # W8-B: keyed by taxonomy file_category (the successor to media_type). A NULL
    # category (a not-yet-(re)scanned row) buckets under "unclassified".
    by_type = {(cat or "unclassified"): {"count": c, "bytes": int(b)} for cat, c, b in rows}
    # T8: extraction throughput / queue-depth observability (single aggregate
    # read over procrastinate_jobs; cheap, read-only). Exposes extract backlog
    # depth + done/failed counts so operators can watch a large scan drain.
    queues = await queue_snapshot(session)
    # T11: live per-library extraction-error counts (single GIN-indexed aggregate
    # over items.metadata ? '_extract_error'). Authoritative, cheap, read-only.
    errors_by_lib = await extract_error_counts_by_library(session)
    # P9-T7/T8: live Meili health + projection drift (postgres active count vs
    # Meili numberOfDocuments) — the same cheap signal the hourly reconcile sweep
    # acts on. Total/read-only: degrades to healthy=false if Meili is down.
    meili = await meili_snapshot(session)
    # P3-T8: semantic-search coverage (embedded/pending/drift). Off => all zeros.
    semantic = await semantic_snapshot(session)
    # P12-T12: thumbnail-cache storage stats (count/bytes/by_source) + soft budget
    # alarm. Cheap grouped aggregate over the disposable manifest projection.
    thumbs = await _thumbnail_stats(session)
    # FIX-11: filesystem headroom for every watch path + the worst rollup status.
    # os.statvfs on a handful of paths — cheap, synchronous, offloaded so a slow
    # network mount cannot block the event loop.
    from starlette.concurrency import run_in_threadpool

    disk = await run_in_threadpool(_disk_section)
    return {
        "by_type": by_type,
        "queues": queues["queues"],
        "extract": queues["extract"],
        "extract_errors": errors_by_lib,
        "meili": meili,
        "semantic": semantic,
        "thumbs": thumbs,
        "disk": disk,
    }


def _disk_section() -> dict:
    """FIX-11 disk headroom for the dashboard: per-path status + worst rollup.

    ``paths`` is one row per watch target (label/path/free/total/pct_free/status/
    reason); ``status`` is the single worst across them (drives the banner)."""
    from filearr import diskguard

    settings = get_settings()
    statuses = diskguard.monitored_statuses(settings)
    paths = [
        {
            "label": st.get("label", st["path"]),
            "path": st["path"],
            "total": st["total"],
            "free": st["free"],
            "used": st["used"],
            "pct_free": round(st["pct_free"], 2),
            "status": st["status"],
            "reason": st["reason"],
            "is_pg": st.get("is_pg", False),
        }
        for st in statuses
    ]
    return {"status": diskguard.overall_status(statuses), "paths": paths}


@router.get("/system/disk", dependencies=[Depends(require_scope("read"))])
async def system_disk() -> dict:
    """FIX-11: filesystem headroom for every monitored path (admin/read scope).

    Same shape as the ``/stats`` ``disk`` section — ``{status, paths:[...]}`` —
    but a dedicated endpoint so the Jobs/Admin banner (and external monitoring)
    can poll disk alone without the heavier ``/stats`` aggregate. Read-only
    ``os.statvfs`` offloaded to a threadpool so a slow mount never blocks the loop."""
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_disk_section)


class ShareMapEntryOut(BaseModel):
    """One deploy-written mount→share mapping (OPS-T7). Credential-free."""

    container_prefix: str
    share_url: str
    storage_type: str | None = None
    host: str | None = None
    unc: str | None = None


@router.get(
    "/system/share-map",
    response_model=list[ShareMapEntryOut],
    dependencies=[Depends(require_scope("read"))],
)
async def system_share_map() -> list[dict]:
    """OPS-T7: the deploy-time network-share mount map that auto-populates library
    ``share_prefix`` (read scope).

    The Proxmox deploy wizard writes this from the rclone/NFS mounts it configured
    inside the container; the app reads it read-only to resolve a container path
    back to a user-facing network location. Empty list when no map is present
    (feature simply off). Never carries credentials — ``share_url`` is a
    user-facing reference only."""
    from filearr import share_map

    return [e.model_dump() for e in share_map.get_entries()]


class FileGroupOut(BaseModel):
    """One file-group taxonomy entry (search-UI facet + external reference; see
    ``filearr.file_groups``). ``file_category`` is the group's parent category key
    (W8-B replaced the removed ``media_type`` nominal parent); ``extensions`` is the
    sorted bare-extension member list."""

    id: str
    label: str
    file_category: str
    description: str
    extensions: list[str]


@router.get(
    "/system/file-groups",
    response_model=list[FileGroupOut],
    dependencies=[Depends(require_scope("read"))],
)
async def file_groups() -> list[dict]:
    """The file-group taxonomy registry (read scope) — the finer, extension-derived
    similarity layer beneath ``file_category``.

    Returns one ``{id, label, file_category, description, extensions}`` object per
    group, in canonical registry order, for the search-UI ``file_group`` facet and
    external reference. ``file_group`` is a pure projection of the extension (see
    ``search.build_doc`` / ``filearr.file_groups.detect_group``), filterable and
    facet-searchable.

    NOTE: after the extension map changes, run ``POST /system/rebuild-index`` so
    existing search documents are re-projected with their ``file_group`` value —
    newly scanned/updated items get it automatically."""
    from filearr.file_groups import registry_payload

    return registry_payload()


@router.post(
    "/system/rebuild-index",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def rebuild_index_endpoint() -> dict:
    """Trigger a full rebuild of the search index from Postgres (admin scope).

    Defers the ``rebuild_index`` task (P9-T5 shadow-index + atomic swap) and returns
    its Procrastinate ``job_id``. The rebuild runs on the ``index`` queue in the
    worker: it builds a fresh shadow index, backfills it from Postgres truth, then
    atomically swaps it into place -- concurrent searches NEVER see a half-built
    index, and any failure before the swap leaves the live index untouched. This is
    the on-demand handle for a settings/schema migration rollout or a manual
    re-projection (operators kept deferring this task by hand).

    DISK HEADROOM: a rebuild holds BOTH the live and the shadow index copies on
    disk at once (~2x the index size) until the post-swap delete of the old data --
    same LMDB constraint as native compaction. Keep the Meili data volume sized for
    the transient 2x (see the ops runbook / P9-T11); at homelab scale this is
    trivially affordable, but flag it as a sizing input as the corpus grows."""
    from filearr.worker import defer_rebuild_index

    job_id = await defer_rebuild_index()
    return {"job_id": job_id}


@router.post(
    "/system/embed-backfill",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def embed_backfill_endpoint() -> dict:
    """Trigger a semantic-embedding backfill pass (P3-T8, admin scope).

    Defers ``embed_missing``, which enqueues a LOWEST-priority ``embed_item`` for
    each active item lacking a current-fingerprint vector, CAPPED per run
    (``FILEARR_EMBED_BACKFILL_BATCH``) — re-invoke until ``/stats`` reports
    ``semantic.pending == 0``. Requires ``FILEARR_SEMANTIC_ENABLED=true`` (the task
    no-ops otherwise). Returns the Procrastinate ``job_id`` of the backfill task.

    On a first enable over a large corpus this is a background pass (~5.5 h for
    750k items on the live LXC); a full ``rebuild-index`` is NOT needed — each
    embed re-syncs its own item so vectors ride the incremental projection."""
    from filearr.worker import defer_embed_missing

    job_id = await defer_embed_missing()
    return {"job_id": job_id}


@router.get(
    "/system/failed-jobs",
    response_model=FailedJobPage,
    dependencies=[Depends(require_scope("read"))],
)
async def failed_jobs_view(
    limit: int = 25, offset: int = 0, session: AsyncSession = Depends(get_session)
) -> dict:
    """Paginated failed Procrastinate jobs (T11 / FIX-8). Read-only; ``limit``
    capped at 100.

    Returns ``{items, total, limit, offset}`` so the UI can render a real pager
    (the failed-jobs list used to grow unbounded on screen — FIX-8). ``total`` is
    the full failed-row count; ``items`` is the requested page. procrastinate 3.9
    does not persist per-job error text in the DB, so each item's ``error`` is
    null and ``attempted_at`` (last event time) is the actionable signal.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    items = await failed_jobs(session, limit=limit, offset=offset)
    total = await failed_jobs_count(session)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/system/jobs/running",
    dependencies=[Depends(require_scope("read"))],
)
async def jobs_running_view(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Currently-executing Procrastinate jobs (UI-T10). Read-only; capped at 50.

    Shape per job: ``{id, queue, task, args, started_at, seconds_running,
    rel_path}``. ``args`` is an ALLOWLISTED reference subset only (item_id/
    library_id/scan_run_id/rel_path) — never the raw kwargs. ``rel_path`` is
    resolved from ``item_id`` where present so humans see filenames. Returns
    ``[]`` when the procrastinate schema is absent."""
    return await running_jobs(session)


@router.get(
    "/system/jobs/summary",
    dependencies=[Depends(require_scope("read"))],
)
async def jobs_summary_view(session: AsyncSession = Depends(get_session)) -> dict:
    """One composite snapshot the Jobs dashboard polls (UI-T10). Read-only.

    Composes existing helpers: per-queue rollup + flat extract summary, the
    in-flight ``running`` list, the 10 most recent ``failed_recent`` jobs, the
    Meili drift snapshot, and ``scans_running`` (running ScanRuns + library
    name). One URL so the UI polls a single endpoint."""
    return await jobs_summary(session)


@router.post(
    "/system/jobs/reap",
    dependencies=[Depends(require_scope("admin"))],
)
async def reap_stalled_jobs_endpoint() -> dict:
    """Requeue or fail jobs orphaned in ``doing`` by a dead/restarted worker
    (FIX-6, admin scope). Runs the SAME reaper the every-5-minutes maintenance
    tick runs, inline, so an operator does not have to wait for the next tick.

    Prunes stalled worker rows, then for each stalled ``doing`` job: a crashed
    ``scan_library`` is FAILED (its ScanRun is already crash-failed; retrigger
    from the Libraries page), every other orphan is RETRIED back to ``todo`` to
    run again — unless a replacement is already queued under the same
    ``queueing_lock`` (collision), in which case the orphan is FAILED instead.

    Returns ``{reaped, retried, failed, pruned_workers}``."""
    from filearr.worker import proc_app, reap_stalled_jobs_now

    async with proc_app.open_async():
        return await reap_stalled_jobs_now()


class ClearFailedJobs(BaseModel):
    """Body for POST /system/jobs/clear-failed (FIX-8). Optional ``queue`` scopes
    the delete to one Procrastinate queue; omitted clears failed rows in every
    queue."""

    queue: str | None = Field(default=None, max_length=128)


@router.post(
    "/system/jobs/clear-failed",
    dependencies=[Depends(require_scope("admin"))],
)
async def clear_failed_jobs(
    body: ClearFailedJobs | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete failed Procrastinate rows NOW (FIX-8, admin scope).

    The daily ``purge_job_history`` task ages terminal rows out on retention; this
    is the on-demand handle the "Clear failed history" button calls so an operator
    can wipe the accumulated failed list immediately instead of waiting. A single
    set-based ``DELETE ... WHERE status = 'failed'`` (optionally filtered to one
    ``queue``); only ``failed`` rows are touched — todo/doing/succeeded are never
    affected. Returns ``{deleted, queue}`` (``deleted`` = affected row count).
    No-op (``deleted=0``) when the procrastinate schema is absent (fresh DB) so
    the endpoint stays total."""
    queue = body.queue if body else None
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return {"deleted": 0, "queue": queue}
    sql = "DELETE FROM procrastinate_jobs WHERE status = 'failed'"
    params: dict = {}
    if queue is not None:
        sql += " AND queue_name = :queue"
        params["queue"] = queue
    result = await session.execute(text(sql), params)
    await session.commit()
    return {"deleted": result.rowcount or 0, "queue": queue}


class JobPriorityUpdate(BaseModel):
    """Body for POST /system/jobs/priority (UI-T14).

    ``queue`` is the Procrastinate queue name (scan/extract/index/embed/
    maintenance/alerts). ``priority`` is clamped to -100..100 (higher runs
    sooner). ``scope`` is currently only ``"pending"`` -- the adjustment applies
    to jobs still in ``todo`` (a job already ``doing`` is never preempted)."""

    queue: str = Field(min_length=1, max_length=128)
    priority: int = Field(ge=-100, le=100)
    scope: Literal["pending"] = "pending"


@router.post(
    "/system/jobs/priority",
    dependencies=[Depends(require_scope("admin"))],
)
async def set_job_priority(
    body: JobPriorityUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    """Re-prioritise a queue's PENDING jobs (UI-T14, admin scope).

    ``UPDATE procrastinate_jobs SET priority = :p WHERE status = 'todo' AND
    queue_name = :q``. Only ``todo`` jobs are touched -- a job already ``doing``
    keeps the priority it was fetched with (procrastinate never preempts a running
    job), so this reorders the BACKLOG, not in-flight work. The per-task-class
    DEFAULT priorities (applied at defer time) are unchanged; this is a one-shot
    bump of what is already queued. Returns ``{queue, priority, updated}`` where
    ``updated`` is the affected row count. No-op (``updated=0``) when the
    procrastinate schema is absent (fresh DB) so the endpoint stays total."""
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return {"queue": body.queue, "priority": body.priority, "updated": 0}
    result = await session.execute(
        text(
            "UPDATE procrastinate_jobs SET priority = :p "
            "WHERE status = 'todo' AND queue_name = :q"
        ),
        {"p": body.priority, "q": body.queue},
    )
    await session.commit()
    return {
        "queue": body.queue,
        "priority": body.priority,
        "updated": result.rowcount or 0,
    }


#: Batch size for deferring index_sync jobs after a reclassify pass. Bounds how
#: many item ids ride a single ``sync_items`` job (and thus one Meili upsert
#: batch) so a large reclass fans out across many small, retryable jobs instead
#: of one giant payload.
RECLASSIFY_SYNC_BATCH = 1000


@router.post(
    "/system/reclassify-extensions",
    dependencies=[Depends(require_scope("admin"))],
)
async def reclassify_extensions(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Recompute every active item's ``(file_category, file_group)`` from the CURRENT
    taxonomy and re-sync the changed docs (OPS-T4, admin scope; W8-B).

    Existing items keep the classification assigned at their last scan; an edit to
    the taxonomy (add/reparent an extension, add a group/category) only takes effect
    on rescan. This endpoint applies the current taxonomy in place WITHOUT a full
    filesystem rescan: it groups the taxonomy's ``ext -> (category, group)`` map by
    target and runs one set-based ``UPDATE ... WHERE extension IN (...)`` per target
    (extensions are stored bare + lowercased, matching ``taxonomy.detect``), then a
    final pass demotes anything whose extension is unmapped/NULL to
    ``(other, other)``. Sidecars are updated by the same extension rule the scan
    uses, so this stays consistent with a rescan.

    Every changed row is re-projected into Meilisearch via the normal incremental
    ``index_sync`` path, deferred in bounded ``RECLASSIFY_SYNC_BATCH``-sized jobs.
    Returns ``{changed, by_category}`` where ``by_category`` maps each destination
    ``file_category`` to how many rows moved INTO it."""
    from filearr import taxonomy, worker

    # Snapshot the current taxonomy and derive the ext -> (category, group) map.
    tax = await taxonomy.load(session)
    # Group extensions by their (category, group) target so each target is one
    # bounded set-based UPDATE.
    targets: dict[tuple[str, str], list[str]] = {}
    for ext, group in tax.ext_to_group.items():
        category = tax.group_to_category.get(group, taxonomy.CATEGORY_OTHER)
        targets.setdefault((category, group), []).append(ext)
    all_mapped = list(tax.ext_to_group.keys())

    counts: dict[str, int] = {}
    changed_ids: list[str] = []

    for (category, group), exts in targets.items():
        result = await session.execute(
            update(Item)
            .where(
                Item.status == ItemStatus.active,
                Item.extension.in_(exts),
                or_(
                    Item.file_category.is_distinct_from(category),
                    Item.file_group.is_distinct_from(group),
                ),
            )
            .values(file_category=category, file_group=group)
            .returning(Item.id)
        )
        ids = [str(r[0]) for r in result]
        if ids:
            counts[category] = counts.get(category, 0) + len(ids)
            changed_ids.extend(ids)

    # Reconciliation: an item whose extension is NULL or no longer mapped falls back
    # to (other, other) (matches taxonomy.detect). ``NOT IN`` is NULL-blind, so the
    # explicit ``IS NULL`` arm is required to catch extensionless files.
    result = await session.execute(
        update(Item)
        .where(
            Item.status == ItemStatus.active,
            or_(Item.extension.is_(None), Item.extension.notin_(all_mapped)),
            or_(
                Item.file_category.is_distinct_from(taxonomy.CATEGORY_OTHER),
                Item.file_group.is_distinct_from(taxonomy.GROUP_OTHER),
            ),
        )
        .values(file_category=taxonomy.CATEGORY_OTHER, file_group=taxonomy.GROUP_OTHER)
        .returning(Item.id)
    )
    other_ids = [str(r[0]) for r in result]
    if other_ids:
        counts[taxonomy.CATEGORY_OTHER] = counts.get(taxonomy.CATEGORY_OTHER, 0) + len(other_ids)
        changed_ids.extend(other_ids)

    await session.commit()

    # Re-project changed rows through the normal incremental index path, in
    # bounded batches (invariant 1: Meili is a rebuildable projection of PG).
    for i in range(0, len(changed_ids), RECLASSIFY_SYNC_BATCH):
        await worker.defer_index_sync(changed_ids[i : i + RECLASSIFY_SYNC_BATCH])

    return {"changed": len(changed_ids), "by_category": counts}



#: Keyset batch size for the RBAC path_scope backfill (750k-row live catalogs).
RBAC_BACKFILL_BATCH = 1000


@router.post(
    "/system/rbac-backfill",
    dependencies=[Depends(require_scope("admin"))],
)
async def rbac_backfill(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stamp ``items.path_scope`` (the ltree RBAC scope key) for existing rows
    (P6-T2). New/moved items are stamped by the scanner going forward; this is
    the one-shot for a pre-existing catalog. Keyset-paginated by ``id`` in
    ``RBAC_BACKFILL_BATCH`` chunks with a commit per batch (bounded memory + a
    resumable, restart-safe pass over a 750k-row table). Idempotent: only rows
    whose ``path_scope`` is NULL are (re)computed, so a re-run resumes cheaply.

    Returns ``{"stamped": n}`` — the number of rows updated this call."""
    from filearr import rbac

    stamped = 0
    last_id: str | None = None
    while True:
        q = (
            select(Item.id, Item.library_id, Item.rel_path)
            .where(Item.path_scope.is_(None))
            .order_by(Item.id)
            .limit(RBAC_BACKFILL_BATCH)
        )
        if last_id is not None:
            q = q.where(Item.id > last_id)
        rows = (await session.execute(q)).all()
        if not rows:
            break
        for iid, lib_id, rel in rows:
            scope = rbac.path_to_ltree(rel, library_id=lib_id)
            await session.execute(
                update(Item).where(Item.id == iid).values(path_scope=scope)
            )
        last_id = rows[-1][0]
        stamped += len(rows)
        await session.commit()
    return {"stamped": stamped}
