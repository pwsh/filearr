"""Inline scan-pipeline glue for alert rule matching (Phase 8, P8-T5).

This is the runtime bridge between the pure, tested core (:mod:`filearr.alerts.rules`)
and the scan walk (:mod:`filearr.tasks.scan`). It:

* loads the enabled, non-``is_system``, in-scope :class:`~filearr.alerts.rules.AlertRule`
  set **once per scan** (mirroring how ``scan.py`` resolves ``enabled_types`` /
  the hash policy once) and pre-compiles every rule's ``pathspec`` glob, so the
  hot per-file loop only runs cached matches;
* evaluates a :class:`~filearr.alerts.rules.FileEvent` against the loaded set,
  producing :class:`AlertDraft` rows for the matches (rendered payload baked in);
* persists drafts into ``alert_events`` with a **state-derived dedup**: a draft is
  skipped when an *undelivered* row with the same ``(rule_id, dedup_key, item_id)``
  already exists (so the same file event seen twice within a window collapses to
  one row). The dedup also collapses duplicates *within* a single batch.

Design notes (per ``docs/tasks/phase-8-alerting-tasks.md`` P8-T5):

* **Dedup is now race-proof at the DB.** The app-level ``NOT EXISTS`` filter
  collapses duplicates *this* writer can see (in-batch + already-loaded pending
  rows); the partial UNIQUE index on
  ``(rule_id, dedup_key, COALESCE(item_id, nil)) WHERE NOT delivered`` (migration
  ``f3b8d2a41c5e``) plus the ``INSERT ... ON CONFLICT DO NOTHING`` below collapse
  a duplicate a CONCURRENT writer (a scan racing the ops pump, or two scans)
  committed after our SELECT. ``item_id`` is COALESCEd to a nil sentinel so two
  NULL-item ops events for the same group collide as intended.
* **``dedup_key`` is a sha256** of the fixed R1 group vocabulary
  ``(event_type, library_id, rule_id)``; for a digest rule the window *start*
  (:func:`filearr.alerts.windows.assign_window`) is appended so each digest
  bucket is a distinct group.
* Digest windowing / group-wait / repeat all key off these rows at dispatch time
  (:mod:`filearr.tasks.alerts`); this module only writes the match records.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from filearr.alerts.rules import AlertRule, FileEvent, _compile_glob, group_key, match_rule
from filearr.alerts.windows import assign_window
from filearr.models import AlertEvent
from filearr.models import AlertRule as AlertRuleRow


def _rule_from_row(row: AlertRuleRow) -> AlertRule:
    """Project a stored :class:`filearr.models.AlertRule` into the pure dataclass.

    Pre-compiles the glob (warming the shared ``_compile_glob`` cache) so the
    per-file matching loop never pays a compile cost."""
    if row.path_glob:
        _compile_glob(row.path_glob)  # warm the cache once, at load time
    return AlertRule(
        id=str(row.id),
        name=row.name,
        event_types=tuple(row.event_types or ()),
        enabled=row.enabled,
        is_system=row.is_system,
        library_id=str(row.library_id) if row.library_id is not None else None,
        path_glob=row.path_glob,
        hash_change_only=row.hash_change_only,
        group_wait_s=row.group_wait_s,
        digest_window=row.digest_window,
        repeat_interval_s=row.repeat_interval_s,
        threshold_count=row.threshold_count,
        threshold_window_s=row.threshold_window_s,
    )


async def load_enabled_rules(session) -> list[AlertRule]:
    """Load the enabled, non-system file-watch rules ONCE per scan run.

    ``is_system`` operational rules are excluded here — they are driven by their
    own detection hooks (scan-failure / extract-error-spike, P8-T9/T10), not the
    per-file walk. Returns pure :class:`AlertRule` dataclasses with their globs
    pre-compiled; an empty list is the zero-overhead fast path the scan uses to
    skip every capture site."""
    rows = (
        (
            await session.execute(
                select(AlertRuleRow).where(
                    AlertRuleRow.enabled.is_(True),
                    AlertRuleRow.is_system.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_rule_from_row(r) for r in rows]


def compute_dedup_key(
    event_type: str,
    library_id: str | None,
    rule_id: str,
    window_start: datetime | None = None,
) -> str:
    """sha256 of the fixed R1 group vocabulary (+ the digest window start).

    Matches :func:`filearr.alerts.rules.group_key`'s canonical order
    ``(event_type, library_id, rule_id)``. For a digest rule the ISO window start
    is appended so each hourly/daily bucket hashes to a distinct key (its rows are
    a separate group flushed at its own boundary)."""
    parts = [event_type, str(library_id), str(rule_id)]
    if window_start is not None:
        parts.append(window_start.isoformat())
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class AlertDraft:
    """A pending ``alert_events`` insert: one rule match, dedup key precomputed."""

    rule_id: str
    event_type: str
    dedup_key: str
    item_id: str | None = None
    library_id: str | None = None
    payload: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def build_draft(
    rule: AlertRule,
    event: FileEvent,
    item_id: str | None,
    now: datetime,
) -> AlertDraft:
    """Build the :class:`AlertDraft` for a confirmed ``(rule, event)`` match."""
    window_start = (
        assign_window(now, rule.digest_window) if rule.digest_window else None
    )
    et, lib, rid = group_key(rule, event)
    dedup_key = compute_dedup_key(et, lib, rid, window_start)
    payload = {
        "event_type": event.event_type,
        "rel_path": event.rel_path,
        "library_id": event.library_id,
        "rule_name": rule.name,
    }
    return AlertDraft(
        rule_id=rule.id,
        event_type=event.event_type,
        dedup_key=dedup_key,
        item_id=item_id,
        library_id=event.library_id,
        payload=payload,
        occurred_at=now,
    )


def evaluate_event(
    rules: list[AlertRule],
    event: FileEvent,
    item_id: str | None,
    now: datetime | None = None,
) -> list[AlertDraft]:
    """Return a draft for every rule in ``rules`` that ``event`` matches.

    Individually guarded: one malformed rule can never abort the whole capture
    (the scan-side invariant that alert work never fails a scan)."""
    if not rules:
        return []
    now = now or datetime.now(UTC)
    out: list[AlertDraft] = []
    for rule in rules:
        try:
            if match_rule(rule, event):
                out.append(build_draft(rule, event, item_id, now))
        except Exception:  # noqa: BLE001 - a bad rule must not break the scan
            continue
    return out


async def persist_drafts(session, drafts: list[AlertDraft]) -> int:
    """Insert ``drafts`` as ``alert_events`` rows, skipping duplicates.

    A draft is dropped when an **undelivered** ``alert_events`` row already exists
    with the same ``(rule_id, dedup_key, item_id)`` — the state-derived dedup that
    collapses "same event seen twice in a window" to one row (P8-T5 accept). The
    same predicate also dedups duplicates *within* this batch. Returns the number
    of rows actually inserted."""
    if not drafts:
        return 0
    rule_ids = {d.rule_id for d in drafts}
    existing_rows = (
        await session.execute(
            select(
                AlertEvent.rule_id, AlertEvent.dedup_key, AlertEvent.item_id
            ).where(
                AlertEvent.rule_id.in_(rule_ids),
                AlertEvent.delivered.is_(False),
            )
        )
    ).all()
    seen: set[tuple[str, str, str | None]] = {
        (str(r.rule_id), str(r.dedup_key), str(r.item_id) if r.item_id else None)
        for r in existing_rows
    }
    values: list[dict] = []
    for d in drafts:
        key = (str(d.rule_id), str(d.dedup_key), str(d.item_id) if d.item_id else None)
        if key in seen:
            continue
        seen.add(key)
        values.append(
            {
                "rule_id": d.rule_id,
                "item_id": d.item_id,
                "library_id": d.library_id,
                "event_type": d.event_type,
                "dedup_key": d.dedup_key,
                "payload": d.payload,
                "occurred_at": d.occurred_at,
            }
        )
    if not values:
        return 0
    # Race-proof insert: the in-batch/loaded-state ``seen`` filter above collapses
    # duplicates this writer can see; ON CONFLICT DO NOTHING against the partial
    # UNIQUE index (migration f3b8d2a41c5e) collapses a duplicate a CONCURRENT
    # writer committed after our SELECT (invariant 5's TOCTOU, closed at the DB).
    # RETURNING counts only the rows that actually landed.
    stmt = (
        pg_insert(AlertEvent)
        .values(values)
        .on_conflict_do_nothing()
        .returning(AlertEvent.id)
    )
    result = await session.execute(stmt)
    return len(result.all())
