import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, share_map
from filearr.api.scan_paths import _normalize_rel_path
from filearr.db import get_session
from filearr.errors import extract_error_count, failing_items
from filearr.models import Item, Library, ScanRun
from filearr.presets import PRESET_BUNDLES, validate_extension_group_names
from filearr.schedule import InvalidCronError, is_network_path, validate_cron
from filearr.schemas import (
    FailingItem,
    LastScan,
    LibraryIn,
    LibraryOut,
    LibraryUpdate,
    TargetedScanIn,
    TreeResponse,
)
from filearr.search import delete_docs
from filearr.security import PermissionContext, require_permission, require_scope
from filearr.worker import defer_extract, defer_scan, proc_app, scan_job_pending

logger = logging.getLogger("filearr.libraries")

router = APIRouter()


def _library_out(library: Library, last_scan: "LastScan | None" = None) -> LibraryOut:
    """Serialize a Library ORM row to LibraryOut, annotating the OPS-T7 effective
    share prefix (manual override wins; else the deploy mount map covering the
    library root; else none) so callers never hand-maintain ``share_prefix``."""
    out = LibraryOut.model_validate(library)
    value, source = share_map.effective_library_share(library.share_prefix, library.root_path)
    out.share_prefix_effective = value
    out.share_prefix_source = source
    # UI-T15: the Windows-UNC counterpart alongside the URL-ish effective prefix.
    loc, _src = share_map.effective_library_share_location(
        library.share_prefix, library.root_path
    )
    out.share_unc_effective = loc.unc
    if last_scan is not None:
        out.last_scan = last_scan
    return out

# Batch size for the explicit-id Meili delete after a library is dropped. Keeps a
# single delete_documents request bounded even for a multi-hundred-thousand-item
# library (each batch is one Meili task).
_MEILI_DELETE_BATCH = 10_000

# Folder-browse (UI-T12) caps. Folders are capped so a pathological flat tree
# can't return an unbounded distinct-segment list; items are paginated.
_TREE_FOLDER_CAP = 500
_TREE_ITEMS_MAX = 500


def _normalize_browse_path(rel: str | None) -> str:
    """Normalise + validate a browse ``rel_path`` (security-critical, mirrors
    scan_paths._normalize_rel_path).

    Returns a posix-separated, slash-trimmed relative path ('' = library root).
    Raises HTTP 422 on any traversal / absolute / NUL / malformed input BEFORE it
    is interpolated into a LIKE prefix."""
    if rel is None:
        return ""
    raw = rel.strip()
    if "\x00" in raw:
        raise HTTPException(422, "path must not contain NUL")
    if raw.startswith("/") or raw.startswith("\\"):
        raise HTTPException(422, "path must be relative, not absolute")
    if len(raw) >= 2 and raw[1] == ":":
        raise HTTPException(422, "path must be relative, not a drive path")
    norm = raw.replace("\\", "/").strip("/")
    if norm == "":
        return ""
    for seg in norm.split("/"):
        if seg in ("", ".", ".."):
            raise HTTPException(
                422,
                "path must be a normalized relative path "
                "(no empty, '.', or '..' segments)",
            )
    return norm


def _like_escape(s: str) -> str:
    """Escape LIKE metacharacters (%, _, \\) so a browse path segment cannot act
    as a wildcard in the prefix pattern. Paired with ``ESCAPE '\\'`` in the SQL."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_schedule_fields(scan_cron: str | None, watch_mode: bool, root_path: str) -> None:
    """Enforce T5 write-time rules, raising HTTP 422 on violation:

      * scan_cron (when set) must be a cronsim-parseable expression; empty/null
        disables scheduling and is allowed.
      * watch_mode may only be enabled for a LOCAL root — inotify is unreliable
        over SMB/NFS/FUSE-remote, so a network root is refused server-side.
    """
    if scan_cron is not None and scan_cron.strip():
        try:
            validate_cron(scan_cron)
        except InvalidCronError as exc:
            raise HTTPException(422, f"invalid scan_cron: {exc}") from exc
    if watch_mode and is_network_path(root_path):
        raise HTTPException(
            422,
            "watch_mode requires a local filesystem path; "
            f"{root_path!r} is on a network mount (SMB/NFS/FUSE) where inotify is "
            "unreliable. Use scan_cron for network libraries.",
        )


def _resolve_scan_target(root_path: str, rel: str) -> tuple[bool, bool]:
    """Resolve a W9 targeted-scan ``rel`` under ``root_path`` from disk, returning
    ``(exists, is_file)``. Sync helper so the async endpoint keeps ``os.path`` calls
    out of the coroutine (ASYNC240), mirroring ``scan_paths._validate_scan_path``.
    ``rel == ""`` resolves to the library root itself (a directory)."""
    abs_path = os.path.join(root_path, rel) if rel else root_path
    return os.path.exists(abs_path), os.path.isfile(abs_path)


def _validate_preset_entries(names: list[str]) -> None:
    """P2-T5: enforce preset-name validity, raising HTTP 422 on violation.

      * A positive entry must be a known bundle in ``PRESET_BUNDLES``.
      * A negative sentinel ``-name`` opts a library out of a *default-on* bundle
        (the only bundles active without being explicitly listed). Negating a
        non-default or unknown bundle is nonsensical -- there is nothing to
        disable -- and is rejected, so a typo like ``-node_modules_bild`` fails
        loudly instead of silently no-op'ing at scan time.
    """
    bad: list[str] = []
    for entry in names:
        if entry.startswith("-"):
            bundle = PRESET_BUNDLES.get(entry[1:])
            if bundle is None or not bundle.default_enabled:
                bad.append(entry)
        elif entry not in PRESET_BUNDLES:
            bad.append(entry)
    if bad:
        raise HTTPException(422, f"unknown or invalid preset(s): {', '.join(bad)}")


def _validate_group_names(names: list[str]) -> None:
    """P2-T5: 422 if any name is not a known extension group."""
    unknown = validate_extension_group_names(names)
    if unknown:
        raise HTTPException(422, f"unknown extension group(s): {', '.join(unknown)}")


@router.get("", response_model=list[LibraryOut], dependencies=[Depends(require_scope("read"))])
async def list_libraries(session: AsyncSession = Depends(get_session)):
    """List libraries, each annotated with its most-recent scan (FIX-10).

    ``last_scan`` is sourced per-library directly from ``scan_runs`` via a single
    ``DISTINCT ON (library_id)`` query (no N+1), so the Admin page's "Last scan"
    column survives worker restarts / redeploys and is NOT subject to the capped,
    global ``GET /scans`` feed dropping a library's latest run out of its window.
    Terminal non-``finished`` runs (failed/stopped/cancelled) are surfaced too, so
    a failed last scan shows its status instead of "never ran"."""
    libraries = (await session.execute(select(Library))).scalars().all()
    # Latest ScanRun per library in one query. DISTINCT ON keeps the first row per
    # library after ordering by (library_id, started_at DESC) -- i.e. the newest.
    latest_runs = (
        await session.execute(
            select(ScanRun)
            .distinct(ScanRun.library_id)
            .order_by(ScanRun.library_id, ScanRun.started_at.desc())
        )
    ).scalars().all()
    by_library = {run.library_id: run for run in latest_runs}

    out: list[LibraryOut] = []
    for library in libraries:
        run = by_library.get(library.id)
        last_scan = None
        if run is not None:
            stats = run.stats or {}
            last_scan = LastScan(
                started_at=run.started_at,
                finished_at=run.finished_at,
                status=run.status,
                seen=stats.get("seen"),
                new=stats.get("new"),
                changed=stats.get("changed"),
                missing=stats.get("missing"),
                # Why enumerated files were skipped — lets the Libraries page
                # explain a gap between the OS folder count and the item count.
                excluded=stats.get("excluded"),
                excluded_gate=stats.get("excluded_gate"),
                excluded_filtered=stats.get("excluded_filtered"),
                pruned_dirs=stats.get("pruned_dirs"),
                permission_denied=stats.get("permission_denied"),
                bytes_seen=stats.get("bytes_seen"),
                pruned_files=stats.get("pruned_files"),
                pruned_counted=stats.get("pruned_counted"),
                pruned_paths=stats.get("pruned_paths"),
            )
        out.append(_library_out(library, last_scan))
    return out


@router.post("", response_model=LibraryOut, status_code=201,
             dependencies=[Depends(require_scope("admin"))])
async def create_library(body: LibraryIn, session: AsyncSession = Depends(get_session)):
    # Normalise empty-string cron to null (both mean "scheduling disabled").
    scan_cron = body.scan_cron.strip() if body.scan_cron and body.scan_cron.strip() else None
    _validate_schedule_fields(scan_cron, body.watch_mode, body.root_path)
    _validate_preset_entries(body.enabled_presets)
    _validate_group_names(body.enabled_extension_groups)
    # mode="json" flattens the HashPolicy enum to its string value for the Text
    # column (pydantic 'gt=0' already rejected a non-positive hash_full_max_bytes).
    data = body.model_dump(mode="json")
    data["scan_cron"] = scan_cron
    library = Library(**data)
    session.add(library)
    await session.commit()
    await session.refresh(library)
    return _library_out(library)


@router.patch("/{library_id}", response_model=LibraryOut,
              dependencies=[Depends(require_scope("admin"))])
async def update_library(
    library_id: uuid.UUID, body: LibraryUpdate, session: AsyncSession = Depends(get_session)
):
    """Partial update. Absent fields are untouched. scan_cron/watch_mode edits are
    re-validated; the running scheduler tick and watch supervisor pick up the new
    config on their next iteration (no worker restart)."""
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")

    # T7 fix: build the patch set from ``model_fields_set`` explicitly rather than
    # relying on ``exclude_unset``. This distinguishes "field absent" (leave
    # untouched) from "field present and null" (clear it) unambiguously, so an
    # explicit ``{"hash_full_max_bytes": null}`` reliably nulls the column
    # (falling back to the global full-hash ceiling) instead of being silently
    # dropped. ``mode="json"`` flattens the HashPolicy enum to its string value.
    dumped = body.model_dump(mode="json")
    fields = {name: dumped[name] for name in body.model_fields_set}
    if "scan_cron" in fields:
        cron = fields["scan_cron"]
        fields["scan_cron"] = cron.strip() if cron and cron.strip() else None

    # Effective values after applying the patch, for validation.
    eff_cron = fields.get("scan_cron", library.scan_cron)
    eff_watch = fields.get("watch_mode", library.watch_mode)
    eff_root = fields.get("root_path", library.root_path)
    # P5-T4: an agent-owned library is never scanned/watched centrally, so its
    # scan schedule + watch-mode controls are rejected (the scheduler tick and the
    # watch supervisor already exclude it; this stops the config being set at all).
    if library.source_agent_id is not None and (eff_watch or (eff_cron and eff_cron.strip())):
        raise HTTPException(
            422,
            "this library is owned by a remote agent; scan scheduling and watch "
            "mode do not apply to replicated content",
        )
    _validate_schedule_fields(eff_cron, eff_watch, eff_root)
    # W8-B taxonomy-gating fields are NOT NULL; coerce an explicit null to [] (the
    # documented "clear all / include everything" value).
    for _gate in ("enabled_categories", "enabled_groups"):
        if _gate in fields:
            fields[_gate] = fields[_gate] or []
    # P2-T5 indexing-control fields. Coerce an explicit null to [] (the columns
    # are NOT NULL, and `[]` is the documented "clear all" value), then validate.
    if "enabled_presets" in fields:
        fields["enabled_presets"] = fields["enabled_presets"] or []
        _validate_preset_entries(fields["enabled_presets"])
    if "enabled_extension_groups" in fields:
        fields["enabled_extension_groups"] = fields["enabled_extension_groups"] or []
        _validate_group_names(fields["enabled_extension_groups"])

    for key, value in fields.items():
        setattr(library, key, value)
    await session.commit()
    await session.refresh(library)
    return _library_out(library)


@router.get(
    "/{library_id}/tree",
    response_model=TreeResponse,
)
async def browse_tree(
    library_id: uuid.UUID,
    path: str = Query(default="", description="rel_path to browse ('' = root)"),
    limit: int = 100,
    offset: int = 0,
    folders_offset: int = 0,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict:
    """UI-T12 in-page folder navigation.

    Returns the immediate child folders of ``path`` plus the files whose
    containing directory IS ``path`` (paginated). ``folders`` are the distinct
    next path segments among items nested below ``path``; ``items`` are the rows
    whose ``rel_path`` dirname equals ``path`` exactly. Sidecars (they follow
    their parent) and trashed tombstones are excluded from both. ``path`` is
    normalised + traversal-checked (422 on ``..``/absolute/NUL) BEFORE it becomes
    a LIKE prefix, and every LIKE metacharacter in it is escaped."""
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")

    norm = _normalize_browse_path(path)
    limit = max(1, min(limit, _TREE_ITEMS_MAX))
    offset = max(0, offset)
    prefix = "" if norm == "" else norm + "/"
    plen = len(prefix)
    pattern = _like_escape(prefix) + "%"
    params = {"lib": str(library_id), "plen": plen, "pattern": pattern}
    # P6-T4: a scoped principal only sees folders/files they can read. The RBAC
    # predicate is literal-bound (ltree-alphabet-only, injection-safe) and spliced
    # into every raw aggregate below. "" => unrestricted (admin/apikey/auth-off).
    from filearr import rbac_sql

    _scope = rbac_sql.compile_scope_fragment(ctx.sql_clause())
    _scope_and = f" AND ({_scope})" if _scope else ""

    # Folders: distinct first segment of the remainder (rel_path minus the prefix)
    # among items nested at least one level below `path`. The text_pattern_ops
    # index on (library_id, rel_path) serves the anchored LIKE prefix scan.
    folder_rows = (
        await session.execute(
            text(
                "SELECT seg AS name, count(*) AS item_count FROM ("
                "  SELECT split_part(substr(rel_path, :plen + 1), '/', 1) AS seg"
                "  FROM items"
                "  WHERE library_id = :lib AND status <> 'trashed'"
                "    AND sidecar_of IS NULL"
                "    AND rel_path LIKE :pattern ESCAPE '\\'"
                "    AND position('/' in substr(rel_path, :plen + 1)) > 0"
                f"{_scope_and}"
                ") t GROUP BY seg ORDER BY seg LIMIT :cap OFFSET :foff"
            ),
            {**params, "cap": _TREE_FOLDER_CAP, "foff": max(0, folders_offset)},
        )
    ).all()
    folders = [{"name": r.name, "item_count": r.item_count} for r in folder_rows]
    folders_total = (
        await session.execute(
            text(
                "SELECT count(DISTINCT split_part(substr(rel_path, :plen + 1), '/', 1))"
                " FROM items"
                " WHERE library_id = :lib AND status <> 'trashed'"
                "   AND sidecar_of IS NULL"
                "   AND rel_path LIKE :pattern ESCAPE '\\'"
                "   AND position('/' in substr(rel_path, :plen + 1)) > 0"
                f"{_scope_and}"
            ),
            params,
        )
    ).scalar_one()

    # Items directly in `path`: remainder contains no '/' (dirname == path).
    exact_dir = (
        "library_id = :lib AND status <> 'trashed' AND sidecar_of IS NULL"
        " AND rel_path LIKE :pattern ESCAPE '\\'"
        " AND position('/' in substr(rel_path, :plen + 1)) = 0"
        + _scope_and
    )
    total_items = (
        await session.execute(
            text(f"SELECT count(*) FROM items WHERE {exact_dir}"), params
        )
    ).scalar_one()
    item_rows = (
        await session.execute(
            text(
                "SELECT id, rel_path, filename, file_category, file_group, size,"
                " title, year"
                f" FROM items WHERE {exact_dir}"
                " ORDER BY filename LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).all()
    items = [
        {
            "id": r.id,
            "rel_path": r.rel_path,
            "filename": r.filename,
            "file_category": r.file_category,
            "file_group": r.file_group,
            "size": r.size,
            "title": r.title,
            "year": r.year,
        }
        for r in item_rows
    ]

    return {
        "library_id": str(library_id),
        "library_name": library.name,
        "path": norm,
        "folders": folders,
        "folders_total": folders_total,
        "folders_offset": max(0, folders_offset),
        "items": items,
        "total_items": total_items,
    }


@router.get(
    "/{library_id}/errors",
    dependencies=[Depends(require_scope("read"))],
)
async def library_errors(
    library_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """T11: extraction-error surface for a library.

    Returns the live authoritative error ``count`` (GIN-indexed aggregate over
    ``items.metadata ? '_extract_error'``) plus a paginated list of the failing
    ``items`` (id / rel_path / sanitized error). ``limit`` is capped at 100 so a
    caller can't request an unbounded page. 404 if the library is unknown."""
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    count = await extract_error_count(session, str(library_id))
    items = await failing_items(session, str(library_id), limit=limit, offset=offset)
    return {
        "library_id": str(library_id),
        "count": count,
        "items": [FailingItem(**i).model_dump(mode="json") for i in items],
    }


@router.post(
    "/{library_id}/retry-extracts",
    dependencies=[Depends(require_scope("write"))],
)
async def retry_extracts(
    library_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Requeue extraction for every item in a library that previously failed.

    Two populations are covered (belt and braces):

    * **class A/D items** — extraction ran but recorded a ``_extract_error`` in
      ``metadata`` (bad tag value, unreadable date, ...). These have
      ``quick_hash`` SET, so the null-quick_hash self-heal would NOT requeue
      them; this action clears the ``_extract_error`` key (single ``metadata -
      '_extract_error'`` UPDATE) and re-defers them.
    * **class B items** — the extract *job* died at commit on bad file data and
      rolled back, leaving ``quick_hash`` NULL. The existing self-heal already
      covers these, but we include them here too so one button fully drains the
      backlog; duplicate defers are harmless (idempotent extract).

    Only ``active`` items are touched. Returns the number of items requeued.
    """
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")

    lib = str(library_id)
    # FIX-1: the never-hashed (quick_hash IS NULL) arm also matches the entire
    # not-yet-extracted backlog during a live scan, so retrying re-defers hundreds
    # of thousands of DUPLICATE extract jobs (423k observed live). Guard the
    # NULL-hash arm with an anti-join against pending extract jobs: exclude any
    # item that already has a todo/doing job in procrastinate_jobs (matched on
    # args->>'item_id'). The _extract_error arm is left untouched -- an errored
    # item should always be requeued regardless of any in-flight job.
    #
    # to_regclass() guards the case where the procrastinate schema is not yet
    # applied (fresh DB / unit tests without the queue): fall back to the plain
    # predicate rather than erroring on a missing relation.
    has_jobs = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if has_jobs is not None:
        null_hash_arm = (
            "(quick_hash IS NULL AND NOT EXISTS ("
            "  SELECT 1 FROM procrastinate_jobs pj"
            "  WHERE pj.task_name = 'filearr.tasks.extract.extract_item'"
            "  AND pj.args->>'item_id' = items.id::text"
            "  AND pj.status IN ('todo', 'doing')"
            "))"
        )
    else:
        null_hash_arm = "quick_hash IS NULL"
    # Ids of every affected active item (errored OR never-hashed-with-no-pending-job).
    # Single scan; the metadata predicate is served by the GIN index used elsewhere.
    rows = await session.execute(
        text(
            "SELECT id::text FROM items "
            "WHERE library_id = :lib AND status = 'active' "
            f"AND (metadata ? '_extract_error' OR {null_hash_arm})"
        ),
        {"lib": lib},
    )
    ids = [r[0] for r in rows.all()]

    # Clear the stale error marker in one UPDATE (jsonb key removal) so the retry
    # starts from a clean slate; a re-failure re-records it.
    await session.execute(
        text(
            "UPDATE items SET metadata = metadata - '_extract_error' "
            "WHERE library_id = :lib AND status = 'active' "
            "AND metadata ? '_extract_error'"
        ),
        {"lib": lib},
    )
    await session.commit()

    await defer_extract(ids)
    return {"library_id": lib, "retried": len(ids)}


@router.delete(
    "/{library_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_library(
    library_id: uuid.UUID,
    confirm: str = Query(
        ...,
        description="Must exactly equal the library name -- a typed-name confirmation "
        "for this intentional hard-delete.",
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """UI-T2: hard-delete a library (admin scope).

    This is the ONE intentional hard-delete in the product; the scan invariant
    ("scans never hard-delete -- tombstone instead") is untouched -- this is an
    explicit operator action, not a scan.

    Guardrails, in order:
      * 404 if the library is unknown.
      * 422 unless ``?confirm=`` exactly matches the library name (typed-name
        confirmation -- guards against deleting the wrong library).
      * 409 while any ScanRun for it is still ``running`` (deleting mid-scan would
        race the scan worker and orphan its writes).

    On success the row is deleted; Postgres FK ``ON DELETE CASCADE`` removes the
    library's items, scan_runs, scan_paths and item_versions. The Meilisearch
    projection is then pruned by EXPLICIT document id (never a filter delete):
    the item ids are collected BEFORE the DB delete (the rows are gone
    afterwards), and the Meili deletes are issued only AFTER the DB commit so a
    failed commit never removes still-live docs. Returns 204."""
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")

    # Typed-name confirmation (exact match, no trimming/casefold -- a mismatch is
    # a refusal, not a coercion).
    if confirm != library.name:
        raise HTTPException(
            422,
            "confirm must exactly match the library name to delete it "
            f"(expected {library.name!r})",
        )

    # Refuse while a scan is in flight for this library.
    running = (
        await session.execute(
            select(func.count())
            .select_from(ScanRun)
            .where(ScanRun.library_id == library_id,
            ScanRun.status.in_(("running", "stopping")))
        )
    ).scalar_one()
    if running:
        raise HTTPException(
            409,
            "cannot delete a library while a scan is running; cancel or wait for "
            "the scan to finish first",
        )

    # Collect item ids for the Meili prune BEFORE the cascade removes the rows.
    item_ids = [
        str(i)
        for i in (
            await session.execute(select(Item.id).where(Item.library_id == library_id))
        ).scalars()
    ]

    await session.execute(sa_delete(Library).where(Library.id == library_id))
    await session.commit()

    # Prune the disposable projection by explicit id, after the commit succeeded.
    for start in range(0, len(item_ids), _MEILI_DELETE_BATCH):
        await delete_docs(item_ids[start : start + _MEILI_DELETE_BATCH])

    logger.info(
        "library deleted: id=%s name=%r items_removed=%d",
        library_id,
        library.name,
        len(item_ids),
    )
    return Response(status_code=204)


@router.post("/{library_id}/scan", status_code=202,
             dependencies=[Depends(require_scope("write"))])
async def trigger_scan(library_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")
    # P5-T4: an agent-owned library's content is REPLICATED in, never scanned by
    # central (its root_path is an agent-side path this host cannot open). A manual
    # scan would walk an empty/absent tree and tombstone the whole catalog, so it
    # is refused outright.
    if library.source_agent_id is not None:
        raise HTTPException(
            422,
            "this library is owned by a remote agent; its content is replicated "
            "in, not scanned centrally — scanning it is not permitted",
        )
    # FIX-8: dedupe against an unfinished (todo/doing/aborting) full scan for this
    # library BEFORE reaping/deferring. A manual trigger must not stack a second
    # scan job on top of one still queued or genuinely running (worker alive) --
    # that was a contributor to the doing-pile storm. If a live scan is in flight
    # we report it rather than reaping its ScanRun out from under it. FAIL-OPEN:
    # if procrastinate is unreachable we do NOT block an explicit manual trigger
    # (worst case one duplicate, vs. blocking a legitimate operator action).
    try:
        async with proc_app.open_async():
            pending = await scan_job_pending(str(library_id), None)
    except Exception:  # noqa: BLE001 - fail open on any queue-connectivity error
        pending = False
    if pending:
        return {"job_id": None, "already_running": True}
    # No unfinished job: reap any leftover orphaned runs (e.g. worker restarted
    # mid-scan without the reaper having caught up) so the UI unblocks, then defer.
    await session.execute(
        update(ScanRun)
        .where(ScanRun.library_id == library_id,
            ScanRun.status.in_(("running", "stopping")))
        .values(status="failed", finished_at=datetime.now(UTC))
    )
    await session.commit()
    # force=True: the operator explicitly asked for a scan and we just confirmed no
    # unfinished job exists, so bypass defer_scan's (now redundant) dedupe.
    job_id = await defer_scan(str(library_id), force=True)
    return {"job_id": job_id}


@router.post("/{library_id}/scan/targeted", status_code=202,
             dependencies=[Depends(require_scope("write"))])
async def trigger_targeted_scan(
    library_id: uuid.UUID,
    body: TargetedScanIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """W9: targeted rescan of a single FILE or a DIRECTORY (optionally recursive).

    Built for automation that lays a directory (or file) down then asks for it to
    be catalogued WITHOUT a full rescan. The target need NOT already exist in the
    catalog, but it MUST exist on disk at request time -> 404 otherwise (immediate
    feedback; a freshly laid-down dir does exist). The scoped scan ingests any
    new/changed files under the target and tombstones only vanished items WITHIN
    the exact scanned set (a file: at most that one item; a non-recursive dir: only
    its direct children; a recursive dir: its whole subtree) -- never anything
    outside the scope.

    Body: ``{"path": "<rel path>", "recursive": true}``.
      * ``path`` is normalized + traversal-checked (reuses the scan_paths guard):
        absolute / ``..`` / drive / NUL -> 422. Empty path == the whole library
        (a normal full scan).
      * ``recursive`` is ignored / N/A when ``path`` resolves to a file.

    Enqueues via the same scoped ``defer_scan`` used by hot folders, so a duplicate
    targeted scan of the SAME path coalesces (the (library, scope) dedupe) and the
    run streams over the existing scan SSE. 202 + the deferred job id (``scan_id``;
    null when coalesced) + the echoed scope/recursive/is_file. Audited (write scope).
    """
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")
    # P5-T4: an agent-owned library's content is replicated in, never scanned by
    # central (its root_path is an agent-side path this host cannot open) -- refuse,
    # exactly as the full-scan trigger does.
    if library.source_agent_id is not None:
        raise HTTPException(
            422,
            "this library is owned by a remote agent; its content is replicated "
            "in, not scanned centrally — scanning it is not permitted",
        )
    # Security-critical: normalize + traversal-check BEFORE the path is joined to
    # the library root (reuses the vetted scan_paths guard). "" => whole library.
    rel = _normalize_rel_path(body.path)
    recursive = bool(body.recursive)

    # The target must exist on disk NOW (it need not be in the catalog). Resolve
    # under the library root and 404 with an automation-friendly detail if absent.
    exists, is_file = _resolve_scan_target(library.root_path, rel)
    if not exists:
        raise HTTPException(
            404, f"path not found on disk under the library root: {rel}"
        )

    # Empty path == whole-library scan (rel_path=None => a normal full run, sharing
    # the library's scan lock/dedupe); a non-empty path is a scoped run.
    rel_arg = rel or None
    job_id = await defer_scan(str(library_id), rel_path=rel_arg, recursive=recursive)
    coalesced = job_id is None

    await audit.emit(
        audit.SCAN_TARGETED,
        request=request,
        details={
            "library_id": str(library_id),
            "path": rel,
            "recursive": recursive,
            "is_file": is_file,
            "coalesced": coalesced,
        },
    )
    return {
        "scan_id": job_id,
        "library_id": str(library_id),
        "path": rel,
        "recursive": recursive,
        "is_file": is_file,
        "coalesced": coalesced,
    }
