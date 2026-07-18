"""FIX-11: periodic low-space monitor + emergency thumbnail GC.

A single 5-minutely maintenance tick that:

  1. evaluates every watch path (``filearr.diskguard.monitored_statuses``);
  2. fires an OPS ALERT through the existing P8 system-rule machinery
     (``filearr.alerts.ops.emit_low_space``) on a warn/critical transition, and a
     "recovered" clear when a path returns to ok — de-duplicated per device so
     co-located paths never double-alert, and hourly-deduped by the alert layer
     so a flapping volume never storms;
  3. at CRITICAL, triggers an EMERGENCY thumbnail GC pass (``run_thumbnail_gc``
     in aggressive mode — orphan sweep always, plus LRU eviction of valid
     thumbnails down to ``disk_gc_target_free_gb`` when that is configured), the
     one bounded, safe lever we have to reclaim ``/config`` space automatically.

FIX-8/FIX-9 discipline: the periodic task carries **no retry** — a transient
failure is simply re-run on the next tick, and the whole body is wrapped so an
alert/GC fault can never leave the monitor wedged. Transition state is held in a
module-level dict so a warn that persists across ticks does not re-alert every 5
minutes (the hourly alert-dedup is the backstop even across a worker restart that
clears this dict)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from filearr import diskguard
from filearr.config import get_settings
from filearr.worker import proc_app

log = logging.getLogger("filearr.diskmon")

# path -> last observed status ('ok'|'warn'|'critical'). Module-level so a status
# that persists across ticks is not re-alerted; reset on worker restart (the
# alert layer's hourly dedup still prevents a storm regardless).
_LAST_STATUS: dict[str, str] = {}


def _reset_state() -> None:
    """Clear the transition memory (tests)."""
    _LAST_STATUS.clear()


async def run_disk_monitor(
    session_factory=None,
    *,
    now: datetime | None = None,
    state: dict | None = None,
) -> dict:
    """Evaluate every watch path, alert on transitions, emergency-GC at critical.

    Returns ``{statuses, alerts, recoveries, gc}`` for observability/tests.
    ``session_factory``/``state`` are injectable for unit tests; production uses
    the app ``SessionLocal`` and the module ``_LAST_STATUS``."""
    from filearr.alerts.ops import emit_low_space
    from filearr.db import SessionLocal
    from filearr.tasks.thumbs import run_thumbnail_gc

    settings = get_settings()
    now = now or datetime.now(UTC)
    last = _LAST_STATUS if state is None else state
    factory = session_factory or SessionLocal

    statuses = diskguard.monitored_statuses(settings)
    # Force a fresh producer-cache read next time a producer checks, so the guard
    # and the monitor never disagree for longer than one tick.
    diskguard.clear_cache()

    alerts = 0
    recoveries = 0
    any_critical = False
    seen_devices: set = set()

    async with factory() as session:
        for st in statuses:
            path = st["path"]
            status = st["status"]
            prev = last.get(path, diskguard.OK)
            if status == diskguard.CRITICAL:
                any_critical = True

            # De-dupe co-located paths (same filesystem) so one full volume does
            # not emit N identical alerts; the alert layer additionally dedups
            # hourly. A device we have already alerted on this tick is skipped for
            # ALERTING but still updates transition state below.
            dev = st.get("dev")
            dev_seen = dev is not None and dev in seen_devices

            if status in (diskguard.WARN, diskguard.CRITICAL):
                # Fire when this is a transition OR an escalation (warn->critical).
                escalated = diskguard._ORDER[status] > diskguard._ORDER[prev]
                if escalated and not dev_seen:
                    try:
                        if await emit_low_space(
                            session,
                            path=path,
                            label=st.get("label", path),
                            status=status,
                            free=st["free"],
                            total=st["total"],
                            pct_free=st["pct_free"],
                            reason=st["reason"],
                            now=now,
                        ):
                            alerts += 1
                    except Exception:  # noqa: BLE001 - alert must not break monitor
                        log.warning("low-space alert emit failed for %s", path, exc_info=True)
                        await session.rollback()
                    if dev is not None:
                        seen_devices.add(dev)
                # Always log the current low state (throttled by level; ops alert
                # is the user-facing signal).
                log.warning(
                    "disk %s on %s: free=%d (%.1f%%) — %s",
                    status, path, st["free"], st["pct_free"], st["reason"],
                )
            elif prev in (diskguard.WARN, diskguard.CRITICAL) and status == diskguard.OK:
                # Recovery: emit a clear (hourly-deduped) once per path.
                if not dev_seen:
                    try:
                        if await emit_low_space(
                            session,
                            path=path,
                            label=st.get("label", path),
                            status="recovered",
                            free=st["free"],
                            total=st["total"],
                            pct_free=st["pct_free"],
                            reason=st["reason"],
                            now=now,
                        ):
                            recoveries += 1
                    except Exception:  # noqa: BLE001
                        log.warning("recovery alert emit failed for %s", path, exc_info=True)
                        await session.rollback()
                    if dev is not None:
                        seen_devices.add(dev)
                log.info("disk recovered on %s: %.1f%% free", path, st["pct_free"])

            last[path] = status

    # Emergency thumbnail GC at critical (bounded + idempotent; safe to run every
    # tick while critical). Fully wrapped so a GC fault never wedges the monitor.
    gc_result = None
    if any_critical:
        target = int(settings.disk_gc_target_free_gb * diskguard.GB)
        try:
            gc_result = await run_thumbnail_gc(aggressive=True, target_free_bytes=target)
            log.warning("emergency thumbnail GC (disk critical) reclaimed %s", gc_result)
        except Exception:  # noqa: BLE001 - GC must not break the monitor
            log.warning("emergency thumbnail GC failed", exc_info=True)

    return {
        "statuses": statuses,
        "alerts": alerts,
        "recoveries": recoveries,
        "gc": gc_result,
    }


@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.tasks.diskmon.monitor_disk",
    queueing_lock="monitor-disk",  # FIX-8/FIX-9: no retry (5-minutely re-runs)
)
async def monitor_disk(timestamp: int) -> dict:
    """Maintenance tick: evaluate disk, alert on transitions, emergency-GC at
    critical (FIX-11). No-op when ``disk_monitor_enabled`` is false. Returns
    ``{alerts, recoveries, critical, gc_evicted}``."""
    settings = get_settings()
    if not settings.disk_monitor_enabled:
        return {"alerts": 0, "recoveries": 0, "critical": 0, "gc_evicted": 0}
    result = await run_disk_monitor(now=datetime.fromtimestamp(timestamp, tz=UTC))
    critical = sum(1 for s in result["statuses"] if s["status"] == diskguard.CRITICAL)
    gc_evicted = (result.get("gc") or {}).get("evicted", 0)
    return {
        "alerts": result["alerts"],
        "recoveries": result["recoveries"],
        "critical": critical,
        "gc_evicted": gc_evicted,
    }


# Worker-startup disk log (FIX-11). The worker imports this module via
# proc_app.import_paths; the API process does NOT import it, so logging here on
# import runs effectively once at worker boot. Guarded so it can never break the
# import of the task module (which would take the worker down).
try:  # pragma: no cover - import-time side effect, exercised by the worker
    from filearr.worker import log_startup_disk_status as _log_startup

    _log_startup()
except Exception:  # noqa: BLE001
    pass
