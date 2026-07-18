"""Library scan: walk -> diff (mtime+size) -> tombstone -> enqueue extraction.

Change detection follows the researched pattern: mtime+size first-pass filter
(PhotoPrism-style), content hashing only for new/changed files (in extract task),
quick-hash tier for move detection. Deletes are tombstoned, never hard-deleted.
"""

import os
import time
from datetime import UTC, datetime

from pathspec import GitIgnoreSpec
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from filearr import rbac
from filearr.alerts import pipeline
from filearr.alerts.rules import FileEvent
from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.errors import sanitize_error
from filearr.hashpolicy import resolve_hash_policy
from filearr.media_types import detect
from filearr.models import (
    AlertEvent,
    Item,
    ItemStatus,
    Library,
    MediaType,
    ScanRun,
)
from filearr.presets import (
    build_library_spec,
    prune_dir,
    resolve_enabled_extensions,
)
from filearr.sidecar import classify
from filearr.tasks.associate import associate_sidecars
from filearr.tasks.move import detect_moves
from filearr.worker import proc_app

# UI-T14: cap on ids per ``batch_defer_async`` multi-row INSERT so a staged
# scan's single end-of-walk defer of the whole library never runs as one
# oversized defer transaction (the per-batch path passes <=250, one chunk).
DEFER_CHUNK = 1000


class ScanRootError(RuntimeError):
    """The library root is missing, not a directory, or unreadable.

    Raised BEFORE the diff/tombstone phase so a vanished/dead mount (e.g. a
    collapsed FUSE bind that presents as gone or unreadable) aborts the scan
    cleanly as ``failed`` instead of walking an empty tree and tombstoning the
    entire library (architecture invariant 7: a scan that can't see its files
    must fail, never silently mark everything missing)."""


def assert_scannable_root(root: str) -> None:
    """Pre-flight guard: the root must exist, be a directory, and be readable.

    A dead mount typically surfaces as a missing path or an ``os.scandir`` that
    raises (ENOENT / ENOTCONN / EACCES). We probe with ``scandir`` and consume
    exactly one entry so a stale handle that errors only on read is still caught,
    then discard it -- the real walk re-opens the tree. Any OSError here means we
    cannot trust the walk's emptiness, so we refuse to proceed to the diff phase.
    An empty but genuinely-mounted directory passes (there is nothing to tombstone
    that isn't legitimately gone)."""
    if not os.path.isdir(root):
        raise ScanRootError(f"scan root is missing or not a directory: {root!r}")
    try:
        with os.scandir(root) as it:
            for _ in it:
                break
    except OSError as exc:
        raise ScanRootError(
            f"scan root is unreadable: {root!r} ({exc.strerror or exc})"
        ) from exc


def walk(root: str, spec: GitIgnoreSpec, start_rel: str = ""):
    """Parallel-friendly scandir walk. Yields (path, rel, size, mtime).

    Exclusion is driven by a single ``GitIgnoreSpec`` (P2-T1), replacing the old
    two-list ``fnmatch`` AND the hard-coded ``entry.name.startswith(".")`` skip
    (which is now the default-on ``hidden_dotfiles`` preset baked into ``spec``):

    * **Directories** are pruned *before* descent via :func:`presets.prune_dir`
      (a directory-only gitignore pattern OR a signature-verified CACHEDIR.TAG).
      Ruling R1: directory pruning always wins — a pruned tree is never entered,
      so nothing inside it can resurface.
    * **Files** the spec excludes are dropped UNLESS :func:`sidecar.classify`
      claims the file, in which case it is kept as a sidecar (ruling R1, file
      level: sidecar classification runs *before* preset exclusion for a file
      whose parent directory is itself indexed — which, by construction, is every
      file the walk reaches, since a pruned parent is never descended into).

    ``rel`` (path relative to ``root``, posix separators) is built incrementally
    in this synchronous generator so the async scan body never calls
    ``os.path.relpath`` (ASYNC240). ``""`` denotes the root directory itself.

    ``start_rel`` (P2-T6) seeds the walk at a *subtree* of ``root`` (a
    ``scan_paths`` hot folder): the emitted ``rel`` stays relative to ``root``
    (library identity is preserved) but only ``root/start_rel`` and below are
    visited. ``""`` (default) walks the whole library root, i.e. a full scan.
    The start directory itself is never prune-checked (an explicitly-configured
    hot folder is always entered); pruning still applies to its descendants."""
    stack = [start_rel]
    while stack:
        rel_dir = stack.pop()
        current = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if not prune_dir(spec, rel, entry.path):
                            stack.append(rel)
                        continue
                    # File-level exclusion, R1-aware: an excluded file that a
                    # sidecar classifier claims is kept (its parent is indexed).
                    if spec.match_file(rel) and classify(rel) is None:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    yield entry.path, rel, stat.st_size, stat.st_mtime
        except PermissionError:
            continue


def _norm_scope(rel_path: str | None) -> str:
    """Normalise a scan scope rel_path: strip slashes; None/"" => "" (full)."""
    return (rel_path or "").strip("/")


def _under_scope(rel: str, scope: str) -> bool:
    """True if library-relative ``rel`` lies within ``scope`` (segment-aware).

    ``scope == ""`` (a full scan) covers everything, so this is identically True
    and the scoped code paths collapse to today's full-scan behaviour."""
    return scope == "" or rel == scope or rel.startswith(scope + "/")


def _scope_dir_missing(root_path: str, scope: str) -> bool:
    """True when a non-empty ``scope`` subtree does not exist under ``root_path``.

    Sync helper (keeps the ``os.path`` calls out of the async scan body,
    ASYNC240). A full scan (``scope == ""``) is never "missing" here — the dead-
    mount guard :func:`assert_scannable_root` already covers the root."""
    return bool(scope) and not os.path.isdir(os.path.join(root_path, scope))


@proc_app.task(queue="scan", name="filearr.tasks.scan.scan_library")
async def scan_library(library_id: str, rel_path: str | None = None) -> dict:
    """Scan a library. ``rel_path`` (P2-T6) confines the walk/diff to a subtree
    (a ``scan_paths`` hot folder); ``None`` (default) is a full-library scan and
    is byte-for-byte the T5 behaviour."""
    scope = _norm_scope(rel_path)
    async with SessionLocal() as session:
        library = (
            await session.execute(select(Library).where(Library.id == library_id))
        ).scalar_one()
        # rel_path=None marks a FULL scan; a scope string marks a scoped run so
        # the scheduler can tell the two apart when deciding what may race.
        run = ScanRun(
            library_id=library.id,
            rel_path=(scope if rel_path is not None else None),
            stats={"seen": 0, "new": 0, "changed": 0},
        )
        session.add(run)
        await session.commit()

        try:
            return await _scan_body(session, library, run, scope_rel=rel_path)
        except Exception as exc:
            # a crashed scan must never stay 'running' forever
            await session.rollback()
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            # T11: retain the exception message on the failed run (sanitized +
            # length-capped -- the message may embed a crafted filename).
            run.stats = {**(run.stats or {}), "error": sanitize_error(exc)}
            await session.commit()
            # P8-T9: dogfood the alert pipeline off the failed-scan event that
            # already exists. Fully wrapped so an alert-layer failure can NEVER
            # mask the original scan failure (integrity/reliability > the alert
            # feature); the failed ScanRun is already committed above regardless.
            try:
                from filearr.alerts.ops import emit_scan_failure

                await emit_scan_failure(
                    session, library=library, run=run, error=exc
                )
            except Exception:  # noqa: BLE001 - never mask the scan failure
                pass
            raise


async def _scan_body(session, library, run, scope_rel: str | None = None) -> dict:
    # scope == "" => whole library (a full scan, byte-for-byte T5). A non-empty
    # scope confines the WALK and all WRITES/tombstones to that subtree while the
    # `existing` diff map below stays whole-library (read-only), so move detection
    # and sidecar association keep full-library context (ruling R3).
    scope = _norm_scope(scope_rel)
    if True:  # preserve original indentation block

        # Pre-flight (cheapest, fail-fast): refuse to diff against an unreachable
        # root. A dead/collapsed mount that presents as gone or unreadable must
        # abort the scan as failed (crash handler in scan_library) rather than
        # walking an empty tree and tombstoning every item in the library
        # (invariant 7). Done BEFORE any ScanRun.stats mutation or row load so the
        # abort path leaves the run row clean.
        assert_scannable_root(library.root_path)

        # Scoped scan of a subtree that does not exist (yet): a scan_paths row may
        # be pre-created before its folder exists, and a temporarily-absent hot
        # folder must NOT tombstone the items recorded under it (integrity >
        # freshness; the library root passed the dead-mount check above, so this
        # is a genuinely-missing subdir, not a dead mount). Finish clean, writing
        # nothing. A full scan (scope == "") never takes this path.
        if _scope_dir_missing(library.root_path, scope):
            run.status = "finished"
            run.finished_at = datetime.now(UTC)
            run.stats = {**(run.stats or {}), "scope": scope, "scope_missing": True,
                         "seen": 0}
            await session.commit()
            return run.stats

        # Resolve the T7 hash policy ONCE for this run (the 'auto' network probe is
        # a mountinfo parse we do not want to repeat per file) and record it in
        # ScanRun.stats for observability. Every hashing site downstream (move
        # detection here; the per-file extract worker) obeys this decision.
        resolved_policy = resolve_hash_policy(
            declared=library.hash_policy,
            root_path=library.root_path,
            hash_full_max_bytes=library.hash_full_max_bytes,
            global_max_bytes=get_settings().scan_hash_full_max_bytes,
        )
        run.stats = {**(run.stats or {}), "hash_policy": resolved_policy.as_stats()}

        existing = {
            item.rel_path: item
            for item in (
                await session.execute(select(Item).where(Item.library_id == library.id))
            ).scalars()
        }

        seen: set[str] = set()
        new = changed = 0
        enabled = set(library.enabled_types or [])
        # P2-T3: per-MediaType extension-group refinement (ruling R5, union
        # semantics). Resolved ONCE per scan into a MediaType -> allowed-set map;
        # a value of None means "no group refines this type" (all extensions in
        # the bucket allowed -- today's behaviour). Sidecars bypass this gate.
        enabled_types_list = list(library.enabled_types or [])
        enabled_groups = list(library.enabled_extension_groups or [])
        ext_allow = {
            mt: resolve_enabled_extensions(mt, enabled_types_list, enabled_groups)
            for mt in MediaType
        }
        FLUSH_EVERY = 250  # commit + publish progress in batches, not one giant txn
        # UI-T14 staged pipeline: when on, extraction is NOT deferred per batch
        # during the walk -- ``pending_extract`` accumulates every new/changed id
        # for the WHOLE scan and is deferred once, chunked, at scan END (invariant
        # 5 still holds: the end-defer runs after ALL batch commits). When off, the
        # T8 per-batch trickle (defer-after-each-commit) is preserved byte-for-byte.
        staged = get_settings().staged_pipeline
        pending_extract: list[str] = []
        # Rows created this scan (non-sidecar): candidates for move detection at
        # scan end. Kept as a set of ids (survive across batch commits/refresh).
        new_item_ids: list[str] = []
        # P8-T5: load the enabled, non-system file-watch rules ONCE per scan run
        # (same pattern as enabled_types / the hash policy). An empty list is the
        # zero-overhead fast path that short-circuits every capture site below.
        # ALL alert work here is wrapped so a rule/eval/persist failure can NEVER
        # fail a scan (integrity/reliability > the alert feature).
        try:
            alert_rules = await pipeline.load_enabled_rules(session)
        except Exception:  # noqa: BLE001
            alert_rules = []
        alert_drafts: list[pipeline.AlertDraft] = []

        def _capture(event_type, rel_path, item_id, old_hash=None, new_hash=None):
            """Buffer alert drafts for one classified transition (no-op if no rules).

            new_hash is intentionally None at walk time for 'modified': content is
            re-hashed later by the extract worker, so a hash_change_only rule
            (which needs BOTH hashes known and different) correctly does NOT fire
            here — it can only be evaluated once the new hash exists."""
            if not alert_rules:
                return
            try:
                ev = FileEvent(
                    event_type=event_type,
                    library_id=str(library.id),
                    rel_path=rel_path,
                    old_hash=old_hash,
                    new_hash=new_hash,
                )
                alert_drafts.extend(
                    pipeline.evaluate_event(alert_rules, ev, item_id)
                )
            except Exception:  # noqa: BLE001
                pass

        async def publish_progress() -> bool:
            """Commit batch + progress, THEN defer extraction for committed rows
            (deferring before commit lets workers race uncommitted items).
            Returns False if the scan was cancelled."""
            run.stats = {
                "hash_policy": resolved_policy.as_stats(),
                "seen": len(seen),
                "new": new,
                "changed": changed,
            }
            if alert_rules and alert_drafts:
                try:
                    await pipeline.persist_drafts(session, alert_drafts)
                except Exception:  # noqa: BLE001
                    pass
                alert_drafts.clear()
            await session.commit()
            # Staged mode holds ALL extraction until scan end (one chunked defer
            # after the final commit); non-staged trickles each committed batch out
            # now (the T8 behaviour). Either way the defer is AFTER the commit.
            if not staged:
                await _defer_extract_batch(pending_extract, str(run.id))
                pending_extract.clear()
            await session.refresh(run)
            return run.status == "running"

        # Single GitIgnoreSpec (P2-T1): union of effective presets +
        # exclude_globs, with include_globs as gitignore negations. Built once
        # per scan; the walk applies it per directory (prune) and per file.
        spec = build_library_spec(library)
        # UI-T13: set True when a graceful stop is requested mid-walk (see the
        # between-batch check below). Distinct from cancel: it breaks the walk
        # cleanly and runs a RESTRICTED wrap-up instead of aborting.
        stop_requested = False
        walk_started = time.monotonic()
        for path, rel, size, mtime_ts in walk(library.root_path, spec, start_rel=scope):
            media_type = detect(path)
            # Sidecars (.nfo / poster.jpg / -thumb / *_JRSidecar.xml, ...) are always
            # ingested regardless of enabled_types: they are bookkeeping rows that get
            # linked to a parent and hidden from default search. Skipping them here
            # would let an episode's .nfo/thumb reappear as stray top-level hits.
            is_sidecar = classify(rel) is not None
            if enabled and not is_sidecar and media_type.value not in enabled:
                continue  # user excluded this media type for this library
            # P2-T3: extension-group refinement within an enabled type (R5
            # union). A non-None allow-set narrows the type to that union; a
            # file whose extension is not in it is skipped. Sidecars bypass
            # (bookkeeping rows are always ingested, like the enabled_types gate).
            if not is_sidecar:
                allowed_exts = ext_allow[media_type]
                if allowed_exts is not None and (
                    os.path.splitext(path)[1].lstrip(".").lower() not in allowed_exts
                ):
                    continue
            seen.add(rel)
            mtime = datetime.fromtimestamp(mtime_ts, tz=UTC)
            item = existing.get(rel)
            if item is None:
                item = Item(
                    library_id=library.id,
                    media_type=media_type,
                    path=path,
                    rel_path=rel,
                    filename=os.path.basename(path),
                    extension=os.path.splitext(path)[1].lstrip(".").lower() or None,
                    size=size,
                    mtime=mtime,
                    # P6-T2: stamp the ltree RBAC scope key on create (the single
                    # natural place; moves restamp it in tasks.move).
                    path_scope=rbac.path_to_ltree(rel, library_id=library.id),
                )
                session.add(item)
                new += 1
                await session.flush()
                if not is_sidecar:
                    pending_extract.append(str(item.id))
                    new_item_ids.append(str(item.id))
                    _capture("created", rel, str(item.id))
            elif item.size != size or item.mtime != mtime:
                item.size, item.mtime = size, mtime
                item.path = path  # refresh absolute path (mount may have moved)
                item.status = ItemStatus.active
                item.last_seen = datetime.now(UTC)
                changed += 1
                if not is_sidecar:
                    pending_extract.append(str(item.id))
                    # old_hash = the prior stored quick_hash; new_hash stays None
                    # (re-hashed async) so hash_change_only rules don't fire here.
                    _capture("modified", rel, str(item.id), old_hash=item.quick_hash)
            else:
                item.last_seen = datetime.now(UTC)
                item.path = path  # keep absolute path current
                if item.status == ItemStatus.missing:
                    item.status = ItemStatus.active
                # committed but never extracted — self-heal (skip sidecars)
                if item.quick_hash is None and not is_sidecar:
                    pending_extract.append(str(item.id))

            if len(seen) % FLUSH_EVERY == 0:
                if not await publish_progress():
                    # UI-T13: the between-batch signal now carries two intents.
                    # A "stopping" status (graceful stop) breaks the walk cleanly
                    # and falls through to the RESTRICTED wrap-up below (move
                    # detection + tombstoning skipped); any other non-running
                    # status ("cancelled") aborts immediately, exactly as before.
                    if run.status == "stopping":
                        stop_requested = True
                        break
                    run.stats = {**run.stats, "aborted": True}
                    await session.commit()
                    return run.stats  # cancelled via API

        # --- Move/rename detection (T2), BEFORE tombstoning --------------------
        # Candidate tombstones: prior-scan rows that vanished from their rel_path
        # AND carry a quick_hash (sidecars have none, so they are excluded and left
        # to the association pass below). New-item rows (this scan) are hashed on
        # demand inside detect_moves. Unambiguous (quick_hash,size)[+content_hash]
        # matches transfer identity onto the original row and drop the duplicate,
        # so a rename preserves id/tags/user_metadata/external_ids/first_seen.
        # R3 (scoped scans): a candidate must be an item we actually WALKED and so
        # confirmed vanished -- i.e. one *under the scope*. `existing` is the full
        # library map (read-only context for move matching + the association pass
        # below), but a relocation whose source lies OUTSIDE the scope cannot be
        # confirmed gone from a subtree walk, so we neither move nor tombstone it
        # here (that would corrupt a still-present out-of-scope row); the next full
        # scan reconciles a cross-scope move. For a full scan (scope == "") the
        # `_under_scope` guard is identically True, so this is exactly T5.
        # UI-T13 graceful stop: the walk was broken off between batches, so the
        # visited set is PARTIAL. Move detection infers a source is *gone* from
        # the walk not revisiting it, but a "vanished" file may simply be one this
        # partial walk never reached -- so the candidate set is unknowable and we
        # skip move detection entirely (empty candidates => the no-op branch
        # below). The next full scan reconciles any real relocation.
        candidates = [] if stop_requested else [
            item
            for rel, item in existing.items()
            if _under_scope(rel, scope)
            and rel not in seen
            and item.status == ItemStatus.active
            and item.quick_hash is not None
        ]
        moved_out: list = []
        if candidates and new_item_ids:
            new_items = list(
                (
                    await session.execute(
                        select(Item).where(Item.id.in_(new_item_ids))
                    )
                ).scalars()
            )
            move_stats = await detect_moves(
                session,
                candidates,
                new_items,
                full_max_bytes=resolved_policy.full_max_bytes,
                compute_content=resolved_policy.compute_content,
                moved_out=moved_out,
            )
            # Duplicate rows that were deleted by a transfer must not be re-queued
            # for extraction: their bytes now live under the surviving id (with the
            # hashes carried over). `session.deleted`-flushed rows are absent from
            # the session's identity map, so test membership via `not in session`.
            deleted_dup_ids = {str(n.id) for n in new_items if n not in session}
            if deleted_dup_ids:
                pending_extract = [i for i in pending_extract if i not in deleted_dup_ids]
        else:
            move_stats = {"moved": 0, "move_ambiguous": 0}

        # P8-T5: a relocation is ONE 'moved' event, not a spurious 'created'.
        # Drop the duplicate row's still-buffered created draft, defensively purge
        # any already-persisted (mid-walk batch) created event for the new path,
        # then emit a 'moved' event for the survivor's new location.
        if alert_rules and moved_out:
            try:
                dup_ids = {mo.duplicate_id for mo in moved_out}
                if dup_ids:
                    alert_drafts[:] = [
                        d for d in alert_drafts if d.item_id not in dup_ids
                    ]
                moved_paths = [mo.survivor_rel_path for mo in moved_out]
                if moved_paths:
                    await session.execute(
                        sa_delete(AlertEvent).where(
                            AlertEvent.event_type == "created",
                            AlertEvent.delivered.is_(False),
                            AlertEvent.library_id == library.id,
                            AlertEvent.payload["rel_path"].astext.in_(moved_paths),
                        )
                    )
                for mo in moved_out:
                    _capture("moved", mo.survivor_rel_path, mo.survivor_id)
            except Exception:  # noqa: BLE001
                pass

        # tombstone unseen files. A candidate whose identity was transferred was
        # repointed onto a *seen* new rel_path, so `item.rel_path not in seen` is
        # False for it and it is correctly skipped (no false tombstone).
        # UI-T13 graceful stop: SKIP tombstoning outright. A partial walk has not
        # visited every path, so "unseen" does NOT prove "gone" -- tombstoning here
        # would mark still-present, merely-unvisited files missing, violating the
        # core integrity property (invariant 4: scans never hard-delete / never
        # false-tombstone). The next scan full diff tombstones whatever is
        # genuinely gone once the whole tree is walked again.
        missing = 0
        if not stop_requested:
            for rel, item in existing.items():
                if (
                    _under_scope(rel, scope)
                    and rel not in seen
                    and item.rel_path not in seen
                    and item.status == ItemStatus.active
                ):
                    item.status = ItemStatus.missing
                    missing += 1
                    _capture("deleted", rel, str(item.id))

        # UI-T13: a graceful stop ends the run "stopped" (terminal); a normal walk
        # ends "finished". The job SUCCEEDS either way (never marked failed), so
        # its queueing locks are released exactly like a completed scan.
        run.status = "stopped" if stop_requested else "finished"
        run.finished_at = datetime.now(UTC)
        # Walk throughput (files/s over the directory walk + diff loop). Uses a
        # monotonic clock so it is immune to wall-clock adjustments; the batched
        # extract jobs run out-of-band on the extract queue and are NOT counted
        # here (that is the queue-depth stat's job).
        walk_elapsed = max(time.monotonic() - walk_started, 1e-6)
        run.stats = {
            "hash_policy": resolved_policy.as_stats(),
            # P2-T6 scope: the subtree this run scanned ("" = full library).
            "scope": scope,
            "seen": len(seen),
            "files_per_s": round(len(seen) / walk_elapsed, 1),
            "walk_seconds": round(walk_elapsed, 2),
            # `moved` rows were counted as `new` during the walk (they were freshly
            # inserted before we recognised them as relocations); reclassify them.
            "new": new - move_stats["moved"],
            "changed": changed,
            "missing": missing,
            "moved": move_stats["moved"],
            "move_ambiguous": move_stats["move_ambiguous"],
            # UI-T13 marker so the API/UI can label a graceful stop distinctly
            # from a full finish (missing/moved are 0 here by construction).
            **({"stopped": True} if stop_requested else {}),
        }
        if alert_rules and alert_drafts:
            try:
                await pipeline.persist_drafts(session, alert_drafts)
            except Exception:  # noqa: BLE001
                pass
            alert_drafts.clear()
        await session.commit()
        # STAGED CRASH-SAFETY (invariant 5 + 7): this is the ONLY defer in staged
        # mode and it runs after EVERY batch commit above, so it can only enqueue
        # committed rows. A run that reaches here is terminal ``finished`` or
        # ``stopped`` (both walked-to-a-clean-stop states) -> defer all accumulated
        # extraction. A CANCELLED or CRASHED scan never reaches this line (it
        # returns/raises earlier), so it defers NOTHING: the strays it committed
        # keep ``quick_hash IS NULL`` and are re-queued by the NEXT scan's in-walk
        # self-heal branch (the ``quick_hash is None`` re-append above; cadence =
        # the library's scan schedule) OR by an operator's retry-extracts action --
        # so no committed item is ever lost, only deferred to the next pass.
        await _defer_extract_batch(pending_extract, str(run.id))
        pending_extract.clear()

        # Associate sidecars to their parents + fold NFO metadata into parents.
        # Runs AFTER the batch commits so every parent row is committed and visible
        # (avoids the worker-race the defer-after-commit rule guards against). The
        # pass is idempotent: it recomputes links from the current row set.
        sidecar_stats = await associate_sidecars(session, library.id)
        await session.commit()
        # Re-index parents whose extracted metadata changed via NFO parsing.
        if sidecar_stats.get("parents_updated"):
            await _reindex_library(session, library.id)
        run.stats = {**run.stats, "sidecars": sidecar_stats}
        await session.commit()
        return run.stats


async def _defer_extract_batch(item_ids: list[str], scan_run_id: str | None = None) -> None:
    """Batch-defer extract jobs for the given committed rows in ONE round-trip
    (Procrastinate 3.9 ``JobDeferrer.batch_defer_async``), instead of one
    ``defer_async`` per file. This preserves the defer-AFTER-commit contract
    exactly -- callers only invoke it once their batch has been committed -- but
    collapses N INSERTs into a single multi-row INSERT, which matters at 5k+
    files (chunked at ``DEFER_CHUNK`` so a staged scan's whole-library end-defer
    is never one oversized transaction). Extract jobs are enqueued on the
    dedicated ``extract`` queue at a negative priority (UI-T14 ``extract_priority``)
    so a fresh scan/cancel is never starved behind the backlog. No-op on an empty
    list (``batch_defer_async`` requires >=1 job).

    ``scan_run_id`` (T11) is threaded onto each job so the extract worker can
    attribute an ``_extract_error`` back to the ScanRun that enqueued it (best-
    effort atomic counter); it is optional so callers that don't have a run (or
    older tests) keep working."""
    if not item_ids:
        return
    settings = get_settings()
    deferrer = proc_app.configure_task(
        "filearr.tasks.extract.extract_item",
        queue=settings.queue_extract,
        priority=settings.extract_priority,
    )
    # Chunk the multi-row INSERT: a staged scan defers the WHOLE library's
    # extraction here in one call, which could be tens of thousands of rows -- one
    # giant defer transaction is avoided by capping each ``batch_defer_async`` at
    # DEFER_CHUNK ids. The per-batch (non-staged) path passes <=250 ids, so this is
    # a single chunk there (unchanged behaviour).
    for start in range(0, len(item_ids), DEFER_CHUNK):
        chunk = item_ids[start : start + DEFER_CHUNK]
        await deferrer.batch_defer_async(
            *({"item_id": i, "scan_run_id": scan_run_id} for i in chunk)
        )


async def _reindex_library(session, library_id) -> None:
    """Defer a Meili re-projection for a library's active items after NFO merges.

    Sidecars are still projected (so an explicit filter can surface them) but carry
    is_sidecar=true and are hidden from default search; this pass just refreshes
    parent docs whose title/plot/metadata changed via NFO parsing."""
    from filearr.models import Item, ItemStatus
    from filearr.tasks.index_sync import sync_items

    ids = [
        str(i)
        for i in (
            await session.execute(
                select(Item.id).where(
                    Item.library_id == library_id,
                    Item.status == ItemStatus.active,
                )
            )
        ).scalars()
    ]
    for start in range(0, len(ids), 1000):
        await sync_items.defer_async(item_ids=ids[start : start + 1000])
