"""Alert dispatch pump (Phase 8, P8-T6/T7/T8/T10 + P8-T15).

A single **state-derived, idempotent** pump drains the ``alert_events`` match
records that :mod:`filearr.tasks.scan` (P8-T5) persists. There is no separate
digest/group state table (brief §3.3): the undelivered rows sharing a
``dedup_key`` *are* the group buffer, ``occurred_at`` seeds group-wait, and the
last ``delivered_at`` seeds group-interval / repeat-interval. Everything the pump
decides is recomputed from the rows every tick, so a crash mid-tick simply
re-derives the same state next minute (invariant 7 — check before acting).

Flow per undelivered, non-terminal group ``(rule_id, dedup_key)``:

1. **Fire decision** — :func:`filearr.alerts.windows.should_fire_now` for
   immediate rules (group-wait then group-interval/repeat); for a ``digest_window``
   rule the group flushes once its :func:`assign_window` bucket boundary has
   passed (the window start is baked into the dedup key, so each bucket is its own
   group).
2. **Ceiling (P8-T15)** — if the rule has already delivered
   ``alert_rule_max_per_hour`` groups in the rolling last hour
   (:func:`filearr.alerts.windows.ceiling_exceeded`), the group is **HELD** (stays
   undelivered, ``last_error`` notes the suppression, logged once per rule per
   hour) — never dropped.
3. **Render** — one grouped/digest body (``alert_digest_max_events`` events, then
   "and N more") via :func:`filearr.alerts.render.render_group`.
4. **Dispatch** — to the rule's ``central``-locality, enabled channels only (R6;
   ``agent``-locality channels are left pending until Phase 5). Secrets are
   decrypted in-process. On success every row in the group is marked
   ``delivered`` + ``delivered_at``. A **retryable** failure increments
   ``delivery_attempts`` (up to ``alert_max_delivery_attempts``, then terminal); a
   **non-retryable** failure goes terminal immediately (``delivery_attempts`` set
   to the ceiling, ``last_error`` recorded).

Terminal = ``NOT delivered AND delivery_attempts >= alert_max_delivery_attempts``
(there is no status column); such rows are skipped by the pump and surfaced as
``failed`` by the events endpoint (P8-T13/T15).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from filearr.alerts.crypto import SecretDecryptError, SecretKeyMissing, get_content_key
from filearr.alerts.dispatch import (
    ChannelDeliveryError,
    send_email,
    send_via_apprise,
    send_webhook_formatted,
)
from filearr.alerts.render import render_group
from filearr.alerts.windows import GroupState, assign_window, ceiling_exceeded, should_fire_now
from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.errors import sanitize_error
from filearr.models import AlertChannel, AlertEvent, AlertRule, AlertRuleChannel
from filearr.worker import proc_app

log = logging.getLogger("filearr.alerts")

# Per-rule "ceiling hit" log throttle: {rule_id: hour_bucket} — log once/rule/hour.
_CEILING_LOGGED: dict[str, datetime] = {}

_DIGEST_DELTA = {"hourly": timedelta(hours=1), "daily": timedelta(days=1)}


def _should_fire(rule: AlertRule, rows, prior_delivered_at, now, group_interval_s):
    """Is this group due to dispatch at ``now``? (state derived from its rows)."""
    first = min(r.occurred_at for r in rows)
    if rule.digest_window:
        start = assign_window(first, rule.digest_window)
        return now >= start + _DIGEST_DELTA[rule.digest_window]
    state = GroupState(
        first_match_at=first,
        last_notified_at=prior_delivered_at,
        has_new_matches=True,
        active=True,
    )
    return should_fire_now(
        state, rule.group_wait_s, group_interval_s, rule.repeat_interval_s, now
    )


async def _central_channels(session, rule_id) -> list[AlertChannel]:
    """The rule's enabled channels whose dispatch_locality is 'central' (R6)."""
    rows = (
        (
            await session.execute(
                select(AlertChannel)
                .join(
                    AlertRuleChannel,
                    AlertRuleChannel.channel_id == AlertChannel.id,
                )
                .where(
                    AlertRuleChannel.rule_id == rule_id,
                    AlertChannel.enabled.is_(True),
                    AlertChannel.dispatch_locality == "central",
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _decrypt_config(channel: AlertChannel) -> dict:
    """Return the channel config with its secret sub-fields decrypted in-process.

    Raises :class:`SecretKeyMissing` / :class:`SecretDecryptError` (caller HOLDs
    the group on these — a recoverable config problem, not a delivery failure)."""
    from filearr.alerts.crypto import decrypt_secret

    cfg = dict(channel.config or {})
    secret_fields = {
        "webhook": ("secret",),
        "email": ("password",),
        "apprise": ("url",),
    }.get(channel.type_, ())
    if not secret_fields:
        return cfg
    key = get_content_key()
    for f in secret_fields:
        if cfg.get(f):
            if key is None:
                raise SecretKeyMissing("FILEARR_SECRET_KEY is required to dispatch")
            cfg[f] = decrypt_secret(cfg[f], key)
    return cfg


async def _send_to_channel(channel: AlertChannel, rendered, settings) -> None:
    """Dispatch ``rendered`` to one channel. Raises ChannelDeliveryError on failure."""
    cfg = _decrypt_config(channel)
    if channel.type_ == "webhook":
        url = cfg.get("url")
        if not url:
            raise ChannelDeliveryError("webhook channel missing 'url'", retryable=False)
        await send_webhook_formatted(
            url,
            rendered,
            config=cfg,  # FIX-16: per-channel webhook_format (generic/discord/slack)
            secret=cfg.get("secret"),
            timeout_s=settings.alert_webhook_timeout_s,
            max_response_bytes=settings.alert_webhook_max_response_bytes,
            allow_private=settings.webhook_allow_private_cidrs,
        )
    elif channel.type_ == "email":
        await send_email(cfg, rendered)
    elif channel.type_ == "apprise":
        await send_via_apprise(cfg.get("url", ""), rendered)
    else:
        raise ChannelDeliveryError(
            f"unknown channel type {channel.type_!r}", retryable=False
        )


async def run_pending_dispatch(session, now: datetime | None = None) -> dict:
    """Drain due groups from ``alert_events``. Operates on the passed session and
    commits per group (bounded blast radius + idempotent re-derivation). Returns a
    small stats dict for observability/tests."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    max_attempts = settings.alert_max_delivery_attempts
    group_interval_s = settings.alert_group_interval_s
    max_events = settings.alert_digest_max_events
    ceiling = settings.alert_rule_max_per_hour

    # P8-T10: run the extract-error-spike detector on THIS pump loop (not a second
    # periodic task). It writes its own alert_events rows (group_wait_s=0), which
    # the same drain below then dispatches. Fully wrapped so a detector failure
    # never stalls the dispatch pump.
    try:
        from filearr.alerts.ops import evaluate_extract_error_spike

        await evaluate_extract_error_spike(session, now)
    except Exception:  # noqa: BLE001 - detector must not break dispatch
        log.warning("extract-error-spike evaluation failed", exc_info=True)
        await session.rollback()

    # Undelivered, non-terminal rows only.
    rows = (
        (
            await session.execute(
                select(AlertEvent).where(
                    AlertEvent.delivered.is_(False),
                    AlertEvent.delivery_attempts < max_attempts,
                )
            )
        )
        .scalars()
        .all()
    )

    groups: dict[tuple, list[AlertEvent]] = defaultdict(list)
    for r in rows:
        groups[(str(r.rule_id), r.dedup_key)].append(r)

    stats = {"groups": 0, "delivered": 0, "held": 0, "failed": 0, "retried": 0, "skipped": 0}
    rule_cache: dict[str, AlertRule | None] = {}

    for (rule_id, dedup_key), grp in groups.items():
        stats["groups"] += 1
        if rule_id not in rule_cache:
            rule_cache[rule_id] = (
                await session.execute(
                    select(AlertRule).where(AlertRule.id == rule_id)
                )
            ).scalar_one_or_none()
        rule = rule_cache[rule_id]
        if rule is None or not rule.enabled:
            stats["skipped"] += 1
            continue

        prior_delivered_at = (
            await session.execute(
                select(func.max(AlertEvent.delivered_at)).where(
                    AlertEvent.rule_id == rule_id,
                    AlertEvent.dedup_key == dedup_key,
                    AlertEvent.delivered.is_(True),
                )
            )
        ).scalar_one_or_none()

        if not _should_fire(rule, grp, prior_delivered_at, now, group_interval_s):
            stats["skipped"] += 1
            continue

        # P8-T15 ceiling: count of groups delivered for this rule in the last hour.
        delivered_last_hour = (
            await session.execute(
                select(func.count(func.distinct(AlertEvent.dedup_key))).where(
                    AlertEvent.rule_id == rule_id,
                    AlertEvent.delivered.is_(True),
                    AlertEvent.delivered_at > now - timedelta(hours=1),
                )
            )
        ).scalar_one()
        if ceiling_exceeded(delivered_last_hour, ceiling):
            note = sanitize_error(
                f"suppressed: rule hit hourly dispatch ceiling ({ceiling})"
            )
            for r in grp:
                r.last_error = note  # HOLD — still undelivered, attempts untouched
            hour_bucket = now.replace(minute=0, second=0, microsecond=0)
            if _CEILING_LOGGED.get(rule_id) != hour_bucket:
                log.warning(
                    "alert rule %s hit hourly dispatch ceiling (%d); holding groups",
                    rule_id,
                    ceiling,
                )
                _CEILING_LOGGED[rule_id] = hour_bucket
            stats["held"] += 1
            await session.commit()
            continue

        channels = await _central_channels(session, rule_id)
        if not channels:
            # No reachable central channel (none configured, all disabled, or only
            # agent-locality — R6). Leave pending until one appears / Phase 5.
            stats["skipped"] += 1
            continue

        grp_sorted = sorted(grp, key=lambda r: r.occurred_at)
        rendered = render_group(
            rule_name=rule.name,
            event_type=grp_sorted[0].event_type,
            library_id=str(grp_sorted[0].library_id)
            if grp_sorted[0].library_id is not None
            else None,
            events=[r.payload or {} for r in grp_sorted],
            max_events=max_events,
            digest_window=rule.digest_window,
        )

        try:
            for ch in channels:
                await _send_to_channel(ch, rendered, settings)
        except (SecretKeyMissing, SecretDecryptError) as exc:
            # Recoverable config problem — HOLD (do not consume an attempt).
            note = sanitize_error(f"dispatch held (secret): {exc}")
            for r in grp:
                r.last_error = note
            stats["held"] += 1
            await session.commit()
            continue
        except ChannelDeliveryError as exc:
            note = sanitize_error(exc.detail)
            if exc.retryable:
                for r in grp:
                    r.delivery_attempts += 1
                    r.last_error = note
                stats["retried"] += 1
                if grp[0].delivery_attempts >= max_attempts:
                    stats["failed"] += 1
            else:
                for r in grp:
                    r.delivery_attempts = max_attempts  # terminal immediately
                    r.last_error = note
                stats["failed"] += 1
            await session.commit()
            continue
        except NotImplementedError as exc:
            # e.g. apprise extra absent — a permanent misconfiguration, terminal.
            note = sanitize_error(str(exc))
            for r in grp:
                r.delivery_attempts = max_attempts
                r.last_error = note
            stats["failed"] += 1
            await session.commit()
            continue

        for r in grp:
            r.delivered = True
            r.delivered_at = now
            r.last_error = None
        stats["delivered"] += 1
        await session.commit()

    return stats


@proc_app.task(
    queue="alerts",
    name="filearr.tasks.alerts.dispatch_pending",
    priority=get_settings().alerts_priority,  # UI-T14: user-facing timeliness
)
async def dispatch_pending(timestamp: int | None = None) -> dict:
    """Procrastinate entrypoint: open a session and drain due alert groups."""
    async with SessionLocal() as session:
        return await run_pending_dispatch(session)
