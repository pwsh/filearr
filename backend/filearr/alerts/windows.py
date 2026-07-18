"""Throttle / digest windowing state machine (Phase 8, brief §4).

Inert scaffolding for Phase 8. Pure functions over an explicit ``GroupState``
and a caller-supplied ``now`` (a synthetic clock in tests, ``datetime.now`` in
production) — no DB, no timers. The persistent home of this state is the
``alert_events`` rows themselves (brief §3.3: no separate ring-buffer/digest
table); this module only decides *whether* a group is due given its stored
timestamps.

Vocabulary is Alertmanager/Grafana's, adopted verbatim (brief §2.2, §4.1):

* ``group_wait`` — delay after the **first** match for a quiet group, so
  near-simultaneous matches collapse into one notification (P8-T7).
* ``group_interval`` — minimum wait before notifying again about **new** matches
  that joined an already-notified group.
* ``repeat_interval`` — resend cadence for a still-firing group with **no** new
  matches (``None`` = never repeat).

:func:`assign_window` buckets a timestamp for digest-mode rules (P8-T8).
:func:`ceiling_exceeded` is the P8-T15 global per-rule hourly dispatch ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Reuse the same window vocabulary the rule dataclass validates.
from filearr.alerts.rules import DIGEST_WINDOWS


def assign_window(ts: datetime, window: str) -> datetime:
    """Return the digest-window *start* that ``ts`` falls into (brief §4.3).

    ``hourly`` → truncate to the top of the hour; ``daily`` → truncate to
    midnight (local to whatever tz ``ts`` already carries — this function does
    not convert timezones, it only truncates). Any accumulated match with the
    same ``window_start`` flushes together at the next boundary crossing.
    """
    if window not in DIGEST_WINDOWS:
        raise ValueError(f"unknown digest window {window!r}")
    if window == "hourly":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class GroupState:
    """The stored state of one dedup group, derived from its ``alert_events`` rows.

    * ``first_match_at`` — when the current active cycle's first match arrived
      (drives the ``group_wait`` initial delay).
    * ``last_notified_at`` — when this group last dispatched (``None`` = never).
    * ``has_new_matches`` — are there matches not yet included in a notification?
      (new matches → ``group_interval`` path; none → ``repeat_interval`` path).
    * ``active`` — is the group still firing at all? A fully resolved+delivered
      group is inactive and never fires.
    """

    first_match_at: datetime | None = None
    last_notified_at: datetime | None = None
    has_new_matches: bool = False
    active: bool = True


def should_fire_now(
    state: GroupState,
    group_wait: int,
    group_interval: int,
    repeat_interval: int | None,
    now: datetime,
) -> bool:
    """Alertmanager-style decision: is this group due to dispatch at ``now``?

    All intervals are **seconds** (matching the ``*_s`` DDL columns);
    ``repeat_interval`` may be ``None`` (never repeat). Semantics (brief §4.1):

    * inactive group → never fires;
    * never notified → fire once ``now >= first_match_at + group_wait`` **and**
      there is something pending;
    * already notified, new matches waiting → fire once
      ``now >= last_notified_at + group_interval``;
    * already notified, nothing new (still firing) → fire only if
      ``repeat_interval`` is set and ``now >= last_notified_at + repeat_interval``.
    """
    if not state.active:
        return False

    if state.last_notified_at is None:
        if not state.has_new_matches or state.first_match_at is None:
            return False
        return now >= state.first_match_at + timedelta(seconds=group_wait)

    if state.has_new_matches:
        return now >= state.last_notified_at + timedelta(seconds=group_interval)

    if repeat_interval is None:
        return False
    return now >= state.last_notified_at + timedelta(seconds=repeat_interval)


def ceiling_exceeded(count_last_hour: int, limit: int | None) -> bool:
    """P8-T15 storm safety net: has this rule hit its hourly dispatch ceiling?

    ``limit`` is ``FILEARR_ALERT_RULE_MAX_PER_HOUR`` (``None`` = no ceiling
    configured → never exceeded). Returns ``True`` once ``count_last_hour``
    reaches ``limit`` — i.e. the *next* dispatch would breach it, so callers
    suppress it. This is independent of group-wait/digest (which absorb the
    common case); the ceiling is the last-resort guard against a pathological
    glob (``**/*``) turning one scan into a notification flood (open question
    §11 Q4, R4).
    """
    if limit is None:
        return False
    return count_last_hour >= limit
