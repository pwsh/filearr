"""Watch-mode filesystem watcher (T5).

A watcher observes a library's root with watchfiles (inotify on Linux) and, on
any change, defers a *normal* full scan after a debounce window. We deliberately
reuse the existing scan pipeline (walk -> diff -> tombstone -> extract) rather
than building a separate incremental single-file path: the scan diff is already
mtime+size cheap, move/sidecar detection only make sense with whole-library
context, and one code path is far less risky than two. (Incremental single-file
updates are noted as a roadmap item.)

Design constraints (see CLAUDE.md):
  * inotify is unreliable over SMB/NFS/FUSE-remote, so watch mode is refused for
    network roots. The refusal is enforced at the API layer on write
    (``schedule.is_network_path``); this module re-checks defensively and simply
    declines to start a watcher for a network root.
  * The watcher lifecycle lives in the worker process and must not block the
    async loop: each watcher is its own cancellable asyncio task, and a
    supervisor reconciles the running set against the DB on a poll interval so
    enabling/disabling watch_mode (or editing a root) takes effect without a
    worker restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from watchfiles import awatch

from filearr.schedule import is_network_path

logger = logging.getLogger("filearr.watch")

# Coalesce a burst of events into a single scan. watchfiles already batches
# events per yield; this adds an extra idle window so a large copy that keeps the
# directory busy triggers exactly one scan once it settles.
DEBOUNCE_S = 3.0
# How often the supervisor re-reads library config to start/stop watchers.
RECONCILE_INTERVAL_S = 30.0


async def _watch_library(
    library_id: str,
    root_path: str,
    trigger,
    *,
    stop: asyncio.Event,
    debounce_s: float = DEBOUNCE_S,
) -> None:
    """Watch ``root_path``; call ``trigger(library_id)`` (async) once per settled
    burst of changes. Runs until ``stop`` is set (or awatch raises)."""
    logger.info("watch: starting watcher for library %s at %s", library_id, root_path)
    try:
        async for _changes in awatch(root_path, stop_event=stop, debounce=int(debounce_s * 1000)):
            # awatch's `debounce` already collapses rapid events into one batch;
            # we still guard with an explicit settle sleep so an ongoing large
            # copy doesn't fire mid-transfer. Drain any change that lands during
            # the settle window is handled by the next awatch iteration.
            await asyncio.sleep(debounce_s)
            if stop.is_set():
                break
            try:
                await trigger(library_id)
            except Exception:  # a scan-defer failure must not kill the watcher
                logger.exception("watch: failed to trigger scan for %s", library_id)
    except Exception:
        logger.exception("watch: watcher for %s crashed", library_id)
    finally:
        logger.info("watch: stopped watcher for library %s", library_id)


class WatchSupervisor:
    """Owns the set of per-library watcher tasks and reconciles it against the DB.

    A watcher is (re)started for every enabled library with ``watch_mode=True``
    and a *local* root; watchers for libraries that were disabled, deleted, moved,
    or turned network-mounted are cancelled. ``reconcile`` is idempotent and safe
    to call on a timer.
    """

    def __init__(
        self, session_factory, trigger, *,
        reconcile_interval_s: float = RECONCILE_INTERVAL_S,
    ):
        self._session_factory = session_factory
        self._trigger = trigger
        self._reconcile_interval_s = reconcile_interval_s
        # library_id -> (task, stop_event, root_path)
        self._watchers: dict[str, tuple[asyncio.Task, asyncio.Event, str]] = {}
        self._closing = False

    async def _desired(self) -> dict[str, tuple[str, str, str | None]]:
        """Map of watcher-key -> (abs_path, library_id, rel_path) that SHOULD be
        running. One watcher per distinct effective watch-enabled path (P2-T6):

          * the library ROOT (key ``str(lib.id)``, rel_path ``None``) when the
            library's own ``watch_mode`` is true; and
          * each enabled ``scan_paths`` row with ``watch_mode`` true (key
            ``"<lib.id>\x00<rel_path>"``), watching just that subtree and
            deferring a *scoped* scan on change.

        The network-mount guard is re-checked **per resolved absolute path**, not
        only at the library level: a library root may be local while a specific
        ``scan_paths`` subfolder is itself a separate network bind-mount (exotic,
        but a false "local" would silently enable an unreliable watcher, so we
        refuse defensively — the API refuses the same write, this is the last
        line of defence)."""
        from sqlalchemy import select

        from filearr.models import Library, ScanPath

        desired: dict[str, tuple[str, str, str | None]] = {}
        async with self._session_factory() as session:
            libraries = list(
                (
                    await session.execute(
                        select(Library).where(
                            Library.enabled.is_(True),
                            # P5-T4: agent-owned libraries are never watched by
                            # central (their content is replicated in, and the
                            # root_path is an agent-side path this host cannot see).
                            Library.source_agent_id.is_(None),
                        )
                    )
                ).scalars()
            )
            for lib in libraries:
                if lib.watch_mode:
                    if is_network_path(lib.root_path):
                        logger.warning(
                            "watch: refusing network root for library %s (%s)",
                            lib.id, lib.root_path,
                        )
                    else:
                        desired[str(lib.id)] = (lib.root_path, str(lib.id), None)
                # Per-path watch overrides (watch_mode explicitly true).
                scan_paths = list(
                    (
                        await session.execute(
                            select(ScanPath).where(
                                ScanPath.library_id == lib.id,
                                ScanPath.enabled.is_(True),
                                ScanPath.watch_mode.is_(True),
                            )
                        )
                    ).scalars()
                )
                for sp in scan_paths:
                    abs_path = (
                        os.path.join(lib.root_path, sp.rel_path)
                        if sp.rel_path
                        else lib.root_path
                    )
                    if is_network_path(abs_path):
                        logger.warning(
                            "watch: refusing network scan_path for library %s (%s)",
                            lib.id, abs_path,
                        )
                        continue
                    key = f"{lib.id}\x00{sp.rel_path}"
                    desired[key] = (abs_path, str(lib.id), sp.rel_path)
        return desired

    async def reconcile(self) -> None:
        if self._closing:
            return
        desired = await self._desired()
        # Stop watchers no longer wanted, or whose path changed, or that died.
        for key, (task, _stop, path) in list(self._watchers.items()):
            if key not in desired or desired[key][0] != path or task.done():
                await self._stop_one(key)
        # Start watchers for newly-wanted paths.
        for key, (path, library_id, rel_path) in desired.items():
            if key not in self._watchers:
                self._start_one(key, path, library_id, rel_path)

    def _start_one(
        self, key: str, path: str, library_id: str, rel_path: str | None
    ) -> None:
        stop = asyncio.Event()
        trigger = self._trigger

        async def _bound(_passed_key: str) -> None:
            # The watcher passes its key; the actual scan target is the captured
            # (library_id, rel_path). A None rel_path defers a full-library scan.
            await trigger(library_id, rel_path)

        task = asyncio.create_task(
            _watch_library(key, path, _bound, stop=stop),
            name=f"watch:{key}",
        )
        self._watchers[key] = (task, stop, path)

    async def _stop_one(self, library_id: str) -> None:
        entry = self._watchers.pop(library_id, None)
        if entry is None:
            return
        task, stop, _root = entry
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def run(self) -> None:
        """Reconcile forever on a timer. Cancel to stop (drains all watchers)."""
        try:
            while True:
                await self.reconcile()
                await asyncio.sleep(self._reconcile_interval_s)
        finally:
            await self.close()

    async def close(self) -> None:
        self._closing = True
        for lib_id in list(self._watchers):
            await self._stop_one(lib_id)
