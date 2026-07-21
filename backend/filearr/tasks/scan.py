"""Library scan: walk -> diff (mtime+size) -> tombstone -> enqueue extraction.

Change detection follows the researched pattern: mtime+size first-pass filter
(PhotoPrism-style), content hashing only for new/changed files (in extract task),
quick-hash tier for move detection. Deletes are tombstoned, never hard-deleted.
"""

import os
import stat as stat_mod
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pathspec import GitIgnoreSpec
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from filearr import rbac, taxonomy
from filearr.alerts import pipeline
from filearr.alerts.rules import FileEvent
from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.errors import sanitize_error
from filearr.hashpolicy import resolve_hash_policy
from filearr.models import (
    AlertEvent,
    Item,
    ItemStatus,
    Library,
    ScanRun,
)
from filearr.presets import (
    build_library_spec,
    prune_dir,
)
from filearr.sidecar import classify
from filearr.tasks.associate import associate_sidecars
from filearr.tasks.move import detect_moves
from filearr.worker import proc_app

# UI-T14: cap on ids per ``batch_defer_async`` multi-row INSERT so a staged
# scan's single end-of-walk defer of the whole library never runs as one
# oversized defer transaction (the per-batch path passes <=250, one chunk).
DEFER_CHUNK = 1000


# Cap on the pruned-directory sample stored in ScanRun.stats. Enough to name the
# culprits (.git / .venv / .vs) without letting a pathological tree bloat the blob.
PRUNED_PATH_SAMPLE = 25


@dataclass
class WalkAudit:
    """Why :func:`walk` did not emit files — the scan's self-accounting.

    Every field except ``count_pruned`` is an OUTPUT the walk increments.

    The reconciliation the UI and docs promise is::

        seen + excluded + pruned_files == files on disk

    ...but only when ``count_pruned`` is enabled. Pruned directories are skipped
    *without being enumerated* (that is the entire point of pruning), so by
    default the files inside them are counted NOWHERE and the identity above is
    a lower bound. Live case (2026-07-19): a library reported 77,394 seen + 318
    excluded against 99,694 files on disk — the missing 21,978 were all inside
    dot-directories (``.git``/``.venv``) pruned by the default-on
    ``hidden_dotfiles`` preset, and nothing in the UI could say so.

    Enabling ``count_pruned`` (per-library ``count_pruned_files``) makes the walk
    do a cheap second pass over each pruned subtree — ``scandir`` only, no
    ``stat``, no spec matching, no ingestion — so the identity holds exactly. It
    is opt-in because that pass costs real directory-listing time on a
    network/rclone mount, which is precisely where big pruned trees live.
    """

    excluded_filtered: int = 0
    pruned_dirs: int = 0
    permission_denied: int = 0
    pruned_files: int = 0
    pruned_paths: list[str] = field(default_factory=list)
    # INPUT: enumerate pruned subtrees just to count them (opt-in, see above).
    count_pruned: bool = False


def _count_tree_files(path: str) -> int:
    """Count files under ``path`` as cheaply as possible.

    ``scandir`` only: no ``stat`` (``is_dir`` rides the cached dirent type on
    Linux), no gitignore matching, no sidecar classification, no ORM work. This
    runs over trees we have deliberately chosen NOT to index, so it must stay far
    cheaper than the real walk. Unreadable subdirectories are skipped rather than
    raising — a partial count is better than failing the scan over a tree we are
    not ingesting anyway."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += 1
        except OSError:
            continue
    return total


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


def walk(
    root: str,
    spec: GitIgnoreSpec,
    start_rel: str = "",
    recursive: bool = True,
    audit: "WalkAudit | None" = None,
):
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
    hot folder is always entered); pruning still applies to its descendants.

    ``recursive`` (W9): the default ``True`` descends the whole subtree (today's
    behaviour, byte-for-byte). ``False`` walks only ``root/start_rel`` itself and
    emits its DIRECT-CHILD files -- subdirectories are neither descended into nor
    prune-evaluated -- so a targeted non-recursive scan touches exactly one
    directory level. The exclusion spec + sidecar rules still apply to those
    direct-child files; the emitted ``rel`` stays relative to ``root`` (identity
    preserved, invariant 3).

    ``audit`` (optional) is a caller-owned :class:`WalkAudit` this walk fills in
    so a scan can report *why* files it did not emit were dropped — every
    exclusion path here used to be silent, which made "the folder has 99k files
    but the library shows 77k" impossible to explain. See :class:`WalkAudit` for
    the reconciliation identity and the pruned-subtree caveat.
    """
    stack = [start_rel]
    while stack:
        rel_dir = stack.pop()
        current = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        # Non-recursive: never descend (and never prune-evaluate a
                        # subdir we won't enter) -- direct-child files only.
                        if recursive and not prune_dir(spec, rel, entry.path):
                            stack.append(rel)
                        elif recursive and audit is not None:
                            audit.pruned_dirs += 1
                            if len(audit.pruned_paths) < PRUNED_PATH_SAMPLE:
                                audit.pruned_paths.append(rel)
                            # Opt-in: enumerate the skipped tree purely to count
                            # it, so seen + excluded + pruned_files reconciles
                            # with the on-disk total (WalkAudit).
                            if audit.count_pruned:
                                audit.pruned_files += _count_tree_files(entry.path)
                        continue
                    # File-level exclusion, R1-aware: an excluded file that a
                    # sidecar classifier claims is kept (its parent is indexed).
                    if spec.match_file(rel) and classify(rel) is None:
                        if audit is not None:
                            audit.excluded_filtered += 1
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    yield entry.path, rel, stat.st_size, stat.st_mtime
        except PermissionError:
            if audit is not None:
                audit.permission_denied += 1
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


def _scope_fs_kind(root_path: str, scope: str) -> str:
    """Classify a non-empty scan ``scope`` under ``root_path`` from the filesystem
    as ``'file'`` / ``'dir'`` / ``'absent'`` (W9). Sync helper (keeps ``os.path``
    off the async scan body, ASYNC240)."""
    p = os.path.join(root_path, scope)
    if os.path.isfile(p):
        return "file"
    if os.path.isdir(p):
        return "dir"
    return "absent"


def _walk_one_file(root: str, rel: str, spec: GitIgnoreSpec):
    """Yield exactly the single regular file at ``root/rel`` (a W9 file-scoped
    scan), applying the SAME spec + sidecar rule the directory walk applies per
    file. Yields nothing if the file vanished between scope resolution and here,
    is not a regular file, or the spec excludes it and no sidecar classifier
    claims it. ``rel`` is kept relative to ``root`` (identity preserved)."""
    path = os.path.join(root, rel)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return
    if not stat_mod.S_ISREG(st.st_mode):
        return
    if spec.match_file(rel) and classify(rel) is None:
        return
    yield path, rel, st.st_size, st.st_mtime


def _in_scanned_set(rel: str, scope: str, *, is_file: bool, recursive: bool) -> bool:
    """Whether library-relative ``rel`` is in the EXACT set a scoped scan walked
    -- i.e. the tombstone/move blast radius (W9). This is the invariant that a
    scoped scan may only ever mark ``missing`` items it actually confirmed gone:

      * a **single-file** scope covers only its one path;
      * a **non-recursive dir** scope covers only that dir's DIRECT-child files
        (never a descendant one level deeper);
      * a **recursive dir** scope (or a full scan, ``scope == ""``) covers the
        whole subtree -- today's :func:`_under_scope` behaviour.

    Nothing OUTSIDE the scanned set is ever in-scope, so a sibling / parent /
    out-of-scope descendant is never touched."""
    if is_file:
        return rel == scope
    if recursive:
        return _under_scope(rel, scope)
    # non-recursive directory scope: direct-child files only (no descendants).
    if scope == "":
        return "/" not in rel
    if not rel.startswith(scope + "/"):
        return False
    return "/" not in rel[len(scope) + 1:]


@proc_app.task(queue="scan", name="filearr.tasks.scan.scan_library")
async def scan_library(
    library_id: str, rel_path: str | None = None, recursive: bool = True
) -> dict:
    """Scan a library. ``rel_path`` (P2-T6) confines the walk/diff to a subtree
    (a ``scan_paths`` hot folder) OR, for a W9 targeted scan, a single file;
    ``None`` (default) is a full-library scan and is byte-for-byte the T5
    behaviour. ``recursive`` (W9, additive/defaulted for back-compat with queued
    jobs) is honoured only when the scope resolves to a directory: ``False`` scans
    just that directory's direct-child files and tombstones only among them."""
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
            return await _scan_body(
                session, library, run, scope_rel=rel_path, recursive=recursive
            )
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


async def _scan_body(
    session, library, run, scope_rel: str | None = None, recursive: bool = True
) -> dict:
    # scope == "" => whole library (a full scan, byte-for-byte T5). A non-empty
    # scope confines the WALK and all WRITES/tombstones to that subtree (or, W9, a
    # single file) while the `existing` diff map below stays whole-library (read-
    # only), so move detection and sidecar association keep full-library context
    # (ruling R3). ``recursive`` (W9) applies only to a directory scope.
    scope = _norm_scope(scope_rel)
    if True:  # preserve original indentation block

        # Pre-flight (cheapest, fail-fast): refuse to diff against an unreachable
        # root. A dead/collapsed mount that presents as gone or unreadable must
        # abort the scan as failed (crash handler in scan_library) rather than
        # walking an empty tree and tombstoning every item in the library
        # (invariant 7). Done BEFORE any ScanRun.stats mutation or row load so the
        # abort path leaves the run row clean.
        assert_scannable_root(library.root_path)

        # W9: classify the scope as the whole library / a single FILE / a directory
        # subtree, FROM THE FILESYSTEM. This decides both the walk shape and the
        # tombstone blast radius (`_in_scanned_set`). `recursive` only matters for a
        # directory scope.
        if scope == "":
            scope_is_file = False
        else:
            fs_kind = _scope_fs_kind(library.root_path, scope)
            if fs_kind == "file":
                scope_is_file = True
            elif fs_kind == "dir":
                scope_is_file = False
            else:
                # The scope path is ABSENT on disk at scan time. Disambiguate via
                # the catalog: an item recorded at EXACTLY this rel_path means the
                # scope was a single file that vanished between enqueue and scan ->
                # fall through so that one item is tombstoned (W9: a file scope
                # tombstones at most its one file). Otherwise this is a vanished /
                # never-existed DIRECTORY subtree (a pre-created or temporarily-
                # absent hot folder): it must NOT mass-tombstone the items recorded
                # under it (integrity > freshness; the root passed the dead-mount
                # check above, so this is a genuinely-missing subdir, not a dead
                # mount) -- finish clean, writing nothing. A full scan never lands
                # here (scope == "").
                exact = (
                    await session.execute(
                        select(Item.id)
                        .where(Item.library_id == library.id, Item.rel_path == scope)
                        .limit(1)
                    )
                ).first()
                if exact is not None:
                    scope_is_file = True
                else:
                    run.status = "finished"
                    run.finished_at = datetime.now(UTC)
                    run.stats = {**(run.stats or {}), "scope": scope,
                                 "scope_missing": True, "seen": 0,
                                 "recursive": recursive}
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
        # Total bytes of every file admitted by the gate this run (the on-disk
        # footprint the scan actually walked, NOT a delta). Accumulated here
        # rather than derived later because `Item` has no scan_run_id, so any
        # after-the-fact SUM(size) would have to guess via last_seen and would be
        # contaminated by concurrent scoped scans and the extract worker.
        bytes_seen = 0
        # Why files were NOT ingested. `excluded_gate` is counted here (the
        # taxonomy category/group gate below); the walk fills the rest. Together
        # with `seen` these explain the gap between "files on disk" and "items in
        # the library", which was previously invisible. count_pruned_files is the
        # per-library opt-in that makes the accounting reconcile exactly (it
        # costs an extra directory-listing pass over the pruned trees).
        audit = WalkAudit(count_pruned=bool(library.count_pruned_files))
        excluded_gate = 0
        # W8-B taxonomy gating (replaces the MediaType-keyed enabled_types + the
        # P2-T3 per-type extension-group refinement). A file is INCLUDED iff its
        # file_group is in enabled_groups OR its file_category is in
        # enabled_categories (a selected category admits all its groups). BOTH empty
        # => include everything. Sidecars bypass the gate entirely.
        enabled_categories = set(library.enabled_categories or [])
        enabled_groups = set(library.enabled_groups or [])
        gate_active = bool(enabled_categories or enabled_groups)
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
                # Live total size walked so far. The dashboard divides `seen` by
                # elapsed for files/min; this is the matching size figure.
                "bytes_seen": bytes_seen,
                # Live exclusion tally so a scan in progress already shows why
                # files are being skipped (see the terminal blob for the split).
                "excluded": excluded_gate + audit.excluded_filtered,
                "pruned_files": audit.pruned_files,
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
        # W8-B: load the (cached) taxonomy snapshot ONCE per scan; classifying each
        # file into (file_category, file_group) is then pure dict lookups (no
        # per-file DB I/O), and the same classification drives both the inclusion
        # gate and the stored columns.
        tax = await taxonomy.load(session)
        # W9: a file scope walks exactly its one file; a directory scope (or full
        # scan) walks the subtree, honouring `recursive`.
        if scope_is_file:
            scan_entries = _walk_one_file(library.root_path, scope, spec)
        else:
            scan_entries = walk(
                library.root_path,
                spec,
                start_rel=scope,
                recursive=recursive,
                audit=audit,
            )
        for path, rel, size, mtime_ts in scan_entries:
            # Sidecars (.nfo / poster.jpg / -thumb / *_JRSidecar.xml, ...) are always
            # ingested regardless of the gate: they are bookkeeping rows that get
            # linked to a parent and hidden from default search. Skipping them here
            # would let an episode's .nfo/thumb reappear as stray top-level hits.
            is_sidecar = classify(rel) is not None
            file_category, file_group = tax.detect(path)
            # W8-B inclusion gate: a file is kept iff nothing is selected (BOTH
            # empty) OR its category is enabled OR its group is enabled (selecting a
            # category admits all its groups). Sidecars bypass the gate.
            if gate_active and not is_sidecar and (
                file_category not in enabled_categories
                and file_group not in enabled_groups
            ):
                excluded_gate += 1
                continue  # user excluded this category/group for this library
            seen.add(rel)
            bytes_seen += size
            mtime = datetime.fromtimestamp(mtime_ts, tz=UTC)
            item = existing.get(rel)
            if item is None:
                item = Item(
                    library_id=library.id,
                    # W8-B: stored (category, group) from the DB-backed taxonomy —
                    # the authoritative classification (media_type is gone).
                    file_category=file_category,
                    file_group=file_group,
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
            if _in_scanned_set(rel, scope, is_file=scope_is_file, recursive=recursive)
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
                    _in_scanned_set(rel, scope, is_file=scope_is_file, recursive=recursive)
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
            # W9: the scope kind + recursion mode this run applied.
            "scope_is_file": scope_is_file,
            "recursive": recursive,
            "seen": len(seen),
            "files_per_s": round(len(seen) / walk_elapsed, 1),
            "walk_seconds": round(walk_elapsed, 2),
            # Total on-disk size walked, plus its throughput. Same clock and same
            # exclusions as files_per_s, so the two are directly comparable.
            "bytes_seen": bytes_seen,
            "bytes_per_s": round(bytes_seen / walk_elapsed),
            # --- why files were not ingested ----------------------------------
            # `excluded` = files the walk SAW and dropped, so
            # `seen + excluded + pruned_files` = files on disk. `pruned_files` is
            # only populated when the library opts into count_pruned_files;
            # otherwise pruned subtrees are never enumerated and the identity is
            # a LOWER BOUND — the usual reason an OS folder count is higher.
            "excluded": excluded_gate + audit.excluded_filtered,
            # Excluded because the library's category/group selection did not
            # admit the file (empty selection = admit everything, so 0 here).
            "excluded_gate": excluded_gate,
            # Excluded by the exclusion spec: presets, exclude_globs, dotfiles.
            "excluded_filtered": audit.excluded_filtered,
            "pruned_dirs": audit.pruned_dirs,
            # A SAMPLE (capped) of pruned directories, so the UI can name the
            # culprits (.git / .venv) instead of reporting a bare count.
            "pruned_paths": audit.pruned_paths,
            # Files inside pruned subtrees. 0 AND unreliable unless
            # count_pruned_files is on — `pruned_counted` says which.
            "pruned_files": audit.pruned_files,
            "pruned_counted": audit.count_pruned,
            "permission_denied": audit.permission_denied,
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
