"""Operational (``is_system``) alert rules — seeding + emission (P8-T9/T10).

Phase 8 dogfoods **one** rule engine: the built-in operational alerts are just
``alert_rules`` rows with ``is_system = true`` and their own detection hooks,
sharing the exact ``alert_events`` -> dispatch -> digest pipeline as user
file-watch rules (tasks doc §6). This module owns the two P8 ops rules:

* **P8-T9 scan failure** — :func:`emit_scan_failure` is called from the scan
  crash handler (:mod:`filearr.tasks.scan`) at the exact commit point where the
  ``ScanRun`` is already marked ``failed`` (invariant 7). It is a pure dispatch
  hook off an event that already exists — no new detection logic — and the caller
  wraps it so it can **never** mask the original scan failure.
* **P8-T10 extract-error spike** — :func:`evaluate_extract_error_spike` runs on
  the minutely alert pump (one loop, not a second periodic task). It snapshots
  the **authoritative, GIN-indexed** ``extract_error_counts_by_library()``
  aggregate (:mod:`filearr.errors`, the T11 source of truth) and fires when a
  library's *increase* over the rolling ``threshold_window_s`` exceeds
  ``threshold_count``. Threshold, not anomaly detection (roadmap explicit).

Both are seeded **disabled** by :func:`seed_system_alert_rules` at startup (like
metadata profiles): the rule exists so an admin can attach channels + enable it,
but nothing dispatches until they do. Re-seeding is idempotent and **never**
overwrites an admin's edits (enabled flag, attached channels, threshold, timings)
— it only inserts a missing rule.

Every emitted row is race-proofed by the P8-T5 dedup partial-unique index
(migration ``f3b8d2a41c5e``): the writes use ``INSERT ... ON CONFLICT DO
NOTHING`` so two writers (a scan racing the pump) collapse to one pending row.
The dedup vocabulary is the fixed R1 group key; the spike alert additionally
stamps the hourly digest window into its key so at most one spike alert fires per
library per hour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from filearr.alerts.pipeline import compute_dedup_key
from filearr.alerts.windows import assign_window
from filearr.config import get_settings
from filearr.errors import extract_error_counts_by_library, sanitize_error
from filearr.models import AlertEvent, AlertRule

log = logging.getLogger("filearr.alerts.ops")

# The two ops event-type tokens. Deliberately OUTSIDE the file-watch
# EVENT_TYPES vocabulary (created/modified/deleted/moved): the API's
# _validate_event_types would reject them, but these rows are seeded directly
# (never through the write API) and their match logic is read-only in the UI.
SCAN_FAILED_EVENT = "scan_failed"
EXTRACT_SPIKE_EVENT = "extract_error_spike"
# FIX-11: low-disk-space ops event. Fired by the 5-minutely disk monitor
# (filearr.tasks.diskmon) on a warn/critical transition, and again as a
# "recovered" clear when a path returns to ok. Outside the file-watch vocabulary
# like the other ops events -- seeded directly, never via the write API.
LOW_SPACE_EVENT = "disk_low_space"
# P11-T9: scheduled report delivery failure. Fired by the export delivery path
# (filearr.report_delivery) when a channel refuses/errs after the retry budget.
# Outside the file-watch vocabulary like the other ops events.
REPORT_DELIVERY_EVENT = "report_delivery_failed"
# P8-T11: distributed-agent health ops events. Fired by the 5-minutely agent
# monitor (filearr.tasks.agentmon) — agent_offline on a live->dark transition
# (and a "recovered" clear when the agent is seen again), agent_replication_
# stalled when an agent is alive but its replication watermark has gone quiet.
# Outside the file-watch vocabulary like the other ops events -- seeded directly,
# never via the write API.
AGENT_OFFLINE_EVENT = "agent_offline"
AGENT_STALL_EVENT = "agent_replication_stalled"
# P10-T3: agent stat_check/rehash_check verification mismatch. Fired inline by the
# verify-completion reconcile path (filearr.verify) when an agent reports that a
# hosted item is gone (deleted) or has changed (size/mtime/hash drift) versus the
# central catalog. Outside the file-watch vocabulary like the other ops events --
# seeded directly, never via the write API.
AGENT_VERIFY_MISMATCH_EVENT = "agent_verify_mismatch"

SCAN_FAILED_RULE_NAME = "System: scan failure"
EXTRACT_SPIKE_RULE_NAME = "System: extract-error spike"
LOW_SPACE_RULE_NAME = "System: low disk space"
REPORT_DELIVERY_RULE_NAME = "System: scheduled report delivery failure"
AGENT_OFFLINE_RULE_NAME = "System: agent offline"
AGENT_STALL_RULE_NAME = "System: agent replication stalled"
AGENT_VERIFY_RULE_NAME = "System: agent verification mismatch"

# Rolling per-library extract-error samples for the spike detector: a list of
# (sampled_at, {library_id: total_error_count}). Module-level so it persists
# across ticks within a long-lived worker process; tests inject their own list.
_SPIKE_SAMPLES: list[tuple[datetime, dict[str, int]]] = []


async def seed_system_alert_rules(session_factory=None) -> None:
    """Insert the two ops ``is_system`` rules if absent (idempotent, disabled).

    Called at app startup (lifespan) and from ``scripts.init_db`` after
    migrations, mirroring :func:`filearr.profiles.seed_profiles_to_db`. A rule is
    identified by ``(is_system, name)``; when it already exists the row is left
    **untouched** so an admin's enable-flag, attached channels and tuned
    thresholds/timings survive a redeploy. DB imports are function-local so the
    module stays import-cheap for the pure-unit test surface."""
    from filearr.db import SessionLocal

    factory = session_factory or SessionLocal
    settings = get_settings()
    specs = [
        {
            "name": SCAN_FAILED_RULE_NAME,
            "event_types": [SCAN_FAILED_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
        {
            "name": EXTRACT_SPIKE_RULE_NAME,
            "event_types": [EXTRACT_SPIKE_EVENT],
            "threshold_count": settings.alert_error_spike_threshold,
            "threshold_window_s": settings.alert_error_spike_window_s,
        },
        {
            "name": LOW_SPACE_RULE_NAME,
            "event_types": [LOW_SPACE_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
        {
            "name": REPORT_DELIVERY_RULE_NAME,
            "event_types": [REPORT_DELIVERY_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
        {
            "name": AGENT_OFFLINE_RULE_NAME,
            "event_types": [AGENT_OFFLINE_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
        {
            "name": AGENT_STALL_RULE_NAME,
            "event_types": [AGENT_STALL_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
        {
            "name": AGENT_VERIFY_RULE_NAME,
            "event_types": [AGENT_VERIFY_MISMATCH_EVENT],
            "threshold_count": None,
            "threshold_window_s": None,
        },
    ]
    async with factory() as session:
        for spec in specs:
            existing = (
                await session.execute(
                    select(AlertRule.id).where(
                        AlertRule.is_system.is_(True),
                        AlertRule.name == spec["name"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue  # preserve admin edits — insert-if-absent only
            session.add(
                AlertRule(
                    name=spec["name"],
                    enabled=False,  # disabled until an admin attaches channels
                    is_system=True,
                    library_id=None,  # all libraries
                    path_glob=None,
                    event_types=spec["event_types"],
                    hash_change_only=False,
                    group_by=["event_type", "library_id", "rule_id"],
                    group_wait_s=0,  # ops alerts dispatch promptly
                    digest_window=None,
                    repeat_interval_s=None,
                    threshold_count=spec["threshold_count"],
                    threshold_window_s=spec["threshold_window_s"],
                )
            )
        await session.commit()


async def _first_enabled_system_rule(session, event_type: str) -> AlertRule | None:
    """The enabled ``is_system`` rule carrying ``event_type`` (or None)."""
    return (
        (
            await session.execute(
                select(AlertRule).where(
                    AlertRule.is_system.is_(True),
                    AlertRule.enabled.is_(True),
                    AlertRule.event_types.any(event_type),
                )
            )
        )
        .scalars()
        .first()
    )


async def emit_scan_failure(
    session, *, library, run, error: object, now: datetime | None = None
) -> bool:
    """Persist an ``alert_events`` row for the enabled scan-failure system rule.

    No-op (returns ``False``) when the rule is absent or disabled. Payload carries
    the library name, sanitized error and run id (§P8-T9). The insert is
    ``ON CONFLICT DO NOTHING`` so a repeated failure within the pending window
    collapses to one alert. **Must be called wrapped** — the scan crash handler
    guards it so an alert-layer failure never masks the original scan failure."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, SCAN_FAILED_EVENT)
    if rule is None:
        return False
    lib_id = str(library.id)
    safe_name = sanitize_error(getattr(library, "name", lib_id))
    safe_err = sanitize_error(error)
    dedup_key = compute_dedup_key(SCAN_FAILED_EVENT, lib_id, str(rule.id))
    payload = {
        "event_type": SCAN_FAILED_EVENT,
        "rule_name": rule.name,
        "library_id": lib_id,
        "library_name": safe_name,
        "run_id": str(run.id),
        "error": safe_err,
        "message": f"scan failed for {safe_name}: {safe_err}",
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=None,
            library_id=library.id,
            event_type=SCAN_FAILED_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted


async def evaluate_extract_error_spike(
    session,
    now: datetime | None = None,
    *,
    state: list | None = None,
) -> int:
    """Fire the extract-error-spike system rule for libraries over threshold.

    Snapshots ``extract_error_counts_by_library()`` (the authoritative GIN
    aggregate) each call and compares each library's current total against the
    oldest sample still inside the rolling window: the delta is "errors ADDED in
    the last window". A first observation establishes the baseline (delta 0, no
    fire) so pre-existing errors never trip it at startup; as the window rolls the
    old baseline ages out and a past spike stops counting. One alert per library
    per hour via an hourly-window-stamped dedup key + ON CONFLICT. Returns the
    number of alerts actually inserted."""
    now = now or datetime.now(UTC)
    samples = _SPIKE_SAMPLES if state is None else state
    rule = await _first_enabled_system_rule(session, EXTRACT_SPIKE_EVENT)
    if rule is None:
        return 0
    settings = get_settings()
    threshold = (
        rule.threshold_count
        if rule.threshold_count is not None
        else settings.alert_error_spike_threshold
    )
    window_s = rule.threshold_window_s or settings.alert_error_spike_window_s

    current = await extract_error_counts_by_library(session)
    cutoff = now - timedelta(seconds=window_s)
    # Prune samples older than the window, then baseline = oldest surviving one.
    samples[:] = [(ts, counts) for (ts, counts) in samples if ts >= cutoff]
    baseline = samples[0][1] if samples else current
    samples.append((now, current))

    window_start = assign_window(now, "hourly")
    fired = 0
    for lib, cur in current.items():
        delta = cur - baseline.get(lib, 0)
        if delta <= threshold:
            continue
        dedup_key = compute_dedup_key(
            EXTRACT_SPIKE_EVENT, lib, str(rule.id), window_start
        )
        payload = {
            "event_type": EXTRACT_SPIKE_EVENT,
            "rule_name": rule.name,
            "library_id": lib,
            "delta": delta,
            "threshold": threshold,
            "window_s": window_s,
            "message": (
                f"extract-error spike: {delta} new errors in "
                f"{window_s}s (threshold {threshold})"
            ),
        }
        stmt = (
            pg_insert(AlertEvent)
            .values(
                rule_id=rule.id,
                item_id=None,
                library_id=lib,
                event_type=EXTRACT_SPIKE_EVENT,
                dedup_key=dedup_key,
                payload=payload,
                occurred_at=now,
            )
            .on_conflict_do_nothing()
            .returning(AlertEvent.id)
        )
        if (await session.execute(stmt)).first() is not None:
            fired += 1
    await session.commit()
    return fired


async def emit_low_space(
    session,
    *,
    path: str,
    label: str,
    status: str,
    free: int,
    total: int,
    pct_free: float,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Persist an ``alert_events`` row for the low-disk system rule (FIX-11).

    No-op (returns ``False``) when the rule is absent or disabled. ``status`` is
    ``warn`` | ``critical`` | ``recovered``; the dedup key is stamped with the
    hourly window AND the status so at most one alert per path per severity per
    hour fires (a flapping volume never storms), yet a warn->critical escalation
    or a recovery still gets its own row. The insert is ``ON CONFLICT DO NOTHING``
    so a duplicate tick collapses. Must be called wrapped so an alert-layer fault
    never breaks the monitor loop."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, LOW_SPACE_EVENT)
    if rule is None:
        return False
    window_start = assign_window(now, "hourly")
    # dedup vocabulary: (event, path, rule, hourly-window + status) so warn and
    # critical and recovered for the same path/hour are DISTINCT rows.
    dedup_key = compute_dedup_key(
        LOW_SPACE_EVENT, path, str(rule.id), window_start
    ) + f":{status}"
    if status == "recovered":
        message = f"disk space recovered on {label} ({path}): {pct_free:.1f}% free"
    else:
        message = (
            f"LOW DISK ({status}) on {label} ({path}): "
            f"{free} bytes free ({pct_free:.1f}%) — {reason}"
        )
    payload = {
        "event_type": LOW_SPACE_EVENT,
        "rule_name": rule.name,
        "path": path,
        "label": label,
        "disk_status": status,
        "free": free,
        "total": total,
        "pct_free": round(pct_free, 2),
        "reason": reason,
        "message": message,
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=None,
            library_id=None,
            event_type=LOW_SPACE_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted


async def emit_report_delivery_failure(
    session,
    *,
    schedule_name: str,
    export_id: str,
    channel_name: str,
    error: object,
    now: datetime | None = None,
) -> bool:
    """Persist an ``alert_events`` row for the report-delivery-failure system rule.

    No-op (returns ``False``) when the rule is absent or disabled. Fired by
    :mod:`filearr.report_delivery` when a scheduled report cannot be delivered
    through its channel; mirrors :func:`emit_scan_failure` (ON CONFLICT DO NOTHING,
    hourly-window-stamped dedup so a flapping schedule cannot storm). Must be
    called wrapped so an alert-layer fault never breaks the delivery path."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, REPORT_DELIVERY_EVENT)
    if rule is None:
        return False
    window_start = assign_window(now, "hourly")
    safe_sched = sanitize_error(schedule_name)
    safe_chan = sanitize_error(channel_name)
    safe_err = sanitize_error(error)
    dedup_key = compute_dedup_key(
        REPORT_DELIVERY_EVENT, export_id, str(rule.id), window_start
    )
    payload = {
        "event_type": REPORT_DELIVERY_EVENT,
        "rule_name": rule.name,
        "schedule_name": safe_sched,
        "export_id": export_id,
        "channel_name": safe_chan,
        "error": safe_err,
        "message": (
            f"scheduled report '{safe_sched}' failed to deliver via "
            f"'{safe_chan}': {safe_err}"
        ),
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=None,
            library_id=None,
            event_type=REPORT_DELIVERY_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted


async def emit_agent_offline(
    session,
    *,
    agent_id: str,
    name: str,
    hostname: str,
    last_seen_at: datetime | None,
    offline_seconds: float,
    status: str,
    now: datetime | None = None,
) -> bool:
    """Persist an ``alert_events`` row for the agent-offline system rule (P8-T11).

    No-op (returns ``False``) when the rule is absent or disabled. ``status`` is
    ``offline`` | ``recovered``; the dedup key is stamped with the hourly window
    AND the status so at most one offline alert and one recovery per agent per
    hour fire (a flapping agent never storms), collapsed further by ``ON CONFLICT
    DO NOTHING``. Must be called wrapped so an alert-layer fault never breaks the
    monitor loop. Agent-controlled ``name``/``hostname`` go through
    ``sanitize_error`` (untrusted text never in a template source position)."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, AGENT_OFFLINE_EVENT)
    if rule is None:
        return False
    window_start = assign_window(now, "hourly")
    dedup_key = compute_dedup_key(
        AGENT_OFFLINE_EVENT, agent_id, str(rule.id), window_start
    ) + f":{status}"
    safe_name = sanitize_error(name)
    safe_host = sanitize_error(hostname)
    hours = offline_seconds / 3600.0
    if status == "recovered":
        message = f"agent {safe_name} ({safe_host}) is back online"
    else:
        message = (
            f"agent {safe_name} ({safe_host}) offline for {hours:.1f}h "
            f"(last seen {last_seen_at.isoformat() if last_seen_at else 'never'})"
        )
    payload = {
        "event_type": AGENT_OFFLINE_EVENT,
        "rule_name": rule.name,
        "agent_id": agent_id,
        "agent_name": safe_name,
        "hostname": safe_host,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "offline_status": status,
        "offline_for_h": round(hours, 2),
        "message": message,
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=None,
            library_id=None,
            event_type=AGENT_OFFLINE_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted


async def emit_agent_verify_mismatch(
    session,
    *,
    item_id: str,
    library_id: object | None,
    rel_path: str,
    agent_id: str,
    agent_name: str,
    mismatch: str,
    differed: list[str],
    now: datetime | None = None,
) -> bool:
    """Persist an ``alert_events`` row for the agent-verify-mismatch system rule
    (P10-T3).

    No-op (returns ``False``) when the rule is absent or disabled. ``mismatch`` is
    the categorical outcome (``deleted`` | ``changed``); ``differed`` lists the
    fields that drifted (``size`` / ``mtime`` / ``quick_hash`` / ``content_hash``,
    or ``exists`` for a deletion). The dedup key is stamped with the hourly window
    AND the mismatch category so a deletion and a later content-drift for the same
    item stay DISTINCT rows while repeats within the hour collapse (``ON CONFLICT
    DO NOTHING``). Must be called wrapped so an alert-layer fault never breaks the
    command-completion path. Agent-controlled ``agent_name`` / ``rel_path`` go
    through ``sanitize_error`` (untrusted text never in a template source
    position)."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, AGENT_VERIFY_MISMATCH_EVENT)
    if rule is None:
        return False
    window_start = assign_window(now, "hourly")
    dedup_key = compute_dedup_key(
        AGENT_VERIFY_MISMATCH_EVENT, item_id, str(rule.id), window_start
    ) + f":{mismatch}"
    safe_name = sanitize_error(agent_name)
    safe_path = sanitize_error(rel_path)
    if mismatch == "deleted":
        message = (
            f"agent {safe_name} reports item gone: {safe_path} "
            f"(tombstoned as missing)"
        )
    else:
        message = (
            f"agent {safe_name} reports item changed: {safe_path} "
            f"(differs: {', '.join(differed)})"
        )
    payload = {
        "event_type": AGENT_VERIFY_MISMATCH_EVENT,
        "rule_name": rule.name,
        "item_id": item_id,
        "rel_path": safe_path,
        "agent_id": agent_id,
        "agent_name": safe_name,
        "mismatch": mismatch,
        "differed": differed,
        "message": message,
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=item_id,
            library_id=library_id,
            event_type=AGENT_VERIFY_MISMATCH_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted


async def emit_agent_replication_stall(
    session,
    *,
    agent_id: str,
    name: str,
    hostname: str,
    last_applied_at: datetime | None,
    watermark: datetime | None,
    stall_seconds: float,
    status: str,
    now: datetime | None = None,
) -> bool:
    """Persist an ``alert_events`` row for the replication-stall system rule (P8-T11).

    The sharper sibling of :func:`emit_agent_offline`: the agent is alive (seen
    within the offline threshold) but nothing has been applied for too long.
    No-op (returns ``False``) when the rule is absent or disabled. ``status`` is
    ``stalled`` | ``recovered``; hourly-window + status stamped into the dedup key
    (one stall alert and one recovery per agent per hour) + ``ON CONFLICT DO
    NOTHING``. Must be called wrapped. ``watermark`` is the newest of the agent's
    replication-ledger ``applied_at`` and ``last_reconcile_at``; ``last_applied_at``
    is the ledger side surfaced separately for the payload."""
    now = now or datetime.now(UTC)
    rule = await _first_enabled_system_rule(session, AGENT_STALL_EVENT)
    if rule is None:
        return False
    window_start = assign_window(now, "hourly")
    dedup_key = compute_dedup_key(
        AGENT_STALL_EVENT, agent_id, str(rule.id), window_start
    ) + f":{status}"
    safe_name = sanitize_error(name)
    safe_host = sanitize_error(hostname)
    hours = stall_seconds / 3600.0
    if status == "recovered":
        message = f"agent {safe_name} ({safe_host}) replication resumed"
    else:
        message = (
            f"agent {safe_name} ({safe_host}) alive but replication stalled "
            f"for {hours:.1f}h (last applied "
            f"{watermark.isoformat() if watermark else 'never'})"
        )
    payload = {
        "event_type": AGENT_STALL_EVENT,
        "rule_name": rule.name,
        "agent_id": agent_id,
        "agent_name": safe_name,
        "hostname": safe_host,
        "last_applied_at": last_applied_at.isoformat() if last_applied_at else None,
        "watermark": watermark.isoformat() if watermark else None,
        "stall_status": status,
        "stalled_for_h": round(hours, 2),
        "message": message,
    }
    stmt = (
        pg_insert(AlertEvent)
        .values(
            rule_id=rule.id,
            item_id=None,
            library_id=None,
            event_type=AGENT_STALL_EVENT,
            dedup_key=dedup_key,
            payload=payload,
            occurred_at=now,
        )
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted
