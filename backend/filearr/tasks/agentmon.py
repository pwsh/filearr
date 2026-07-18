"""P8-T11: periodic agent-offline + replication-stall monitor.

A single 5-minutely maintenance tick — the diskmon sibling — that evaluates the
distributed-agent fleet and fires the two SEEDED (disabled-by-default) system
alert rules through the existing P8 ops-alert machinery:

  1. **"System: agent offline"** (``filearr.alerts.ops.emit_agent_offline``) —
     per agent that is *active* (cert bound), *not revoked*, and whose
     ``last_seen_at`` is older than ``FILEARR_AGENT_OFFLINE_ALERT_SECONDS``
     (default 48h). Recovery-clears when the agent is seen again. The threshold
     is DELIBERATELY generous: **offline is a normal agent state** (research
     §7.4) — a laptop that sleeps nightly must never page anyone.
  2. **"System: agent replication stalled"**
     (``filearr.alerts.ops.emit_agent_replication_stall``) — the sharper signal:
     the agent IS alive (``last_seen_at`` within the offline threshold) but its
     newest replication watermark (max of the ledger's ``applied_at`` and
     ``last_reconcile_at``) is older than
     ``FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS`` (default 6h). A fresh
     enrollee (zero ledger rows AND ``last_reconcile_at`` NULL) is guarded out —
     it has never replicated, so it cannot have *stalled*.

Stall is only evaluated for a live agent: an offline agent's replication is
obviously quiet, and offline is already the (softer) signal — we never double-
alert. Candidate selection is one cheap query over the small ``agents`` table
(cert-bound + non-revoked); the newest-ledger-row lookup runs only per surviving
live candidate.

FIX-8/FIX-9 discipline (mirrors diskmon): the periodic carries **no retry** — a
transient fault re-runs on the next tick — and every emit is wrapped so an
alert-layer fault can never wedge the monitor. Transition state (offline / stall,
per agent) is held in a module-level dict so a condition that persists across
ticks does not re-alert every 5 minutes; the alert layer's hourly-window dedup is
the backstop even across a worker restart that clears the dict.

Feature-gated: a total no-op when ``FILEARR_AGENTS_ENABLED`` is false or the
agent tables are absent (``to_regclass`` guard — totality on bare/queue-only DBs,
same as the diskmon/reaper siblings)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from filearr.config import get_settings
from filearr.worker import proc_app

log = logging.getLogger("filearr.agentmon")

# (kind, agent_id) -> bool  where kind in {"offline", "stall"}. Module-level so a
# condition that persists across ticks is not re-alerted; reset on worker restart
# (the alert layer's hourly-window dedup still prevents a storm regardless).
_STATE: dict[tuple[str, str], bool] = {}


def _reset_state() -> None:
    """Clear the transition memory (tests)."""
    _STATE.clear()


async def run_agent_monitor(
    session_factory=None,
    *,
    now: datetime | None = None,
    state: dict | None = None,
) -> dict:
    """Evaluate the fleet, alert on offline/stall transitions, recovery-clear.

    Returns ``{evaluated, offline, offline_recovered, stalled, stall_recovered}``
    for observability/tests. ``session_factory``/``state`` are injectable for unit
    tests; production uses the app ``SessionLocal`` and the module ``_STATE``."""
    from filearr.alerts.ops import emit_agent_offline, emit_agent_replication_stall
    from filearr.db import SessionLocal
    from filearr.models import Agent, AgentReplicationLog

    settings = get_settings()
    now = now or datetime.now(UTC)
    st = _STATE if state is None else state
    factory = session_factory or SessionLocal

    offline_after = timedelta(seconds=settings.agent_offline_alert_seconds)
    stall_after = timedelta(seconds=settings.agent_replication_stall_alert_seconds)

    result = {
        "evaluated": 0,
        "offline": 0,
        "offline_recovered": 0,
        "stalled": 0,
        "stall_recovered": 0,
    }

    async with factory() as session:
        # Totality on a bare/queue-only DB: no agents table => nothing to do.
        reg = (
            await session.execute(
                text("SELECT to_regclass('agents') AS a")
            )
        ).first()
        if reg is None or reg.a is None:
            return result

        # Candidates: cert-bound (active) AND not revoked. A pending-unbound agent
        # (cert_fingerprint IS NULL) has not completed enrollment; a revoked agent
        # is denylisted — neither should ever page.
        agents = (
            (
                await session.execute(
                    select(Agent).where(
                        Agent.cert_fingerprint.is_not(None),
                        Agent.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        for agent in agents:
            result["evaluated"] += 1
            aid = str(agent.id)
            last_seen = agent.last_seen_at

            # --- offline transition ---------------------------------------
            # A NULL last_seen_at agent (bound its cert but never checked in) is
            # treated like a fresh enrollee: no meaningful "offline for Xh", and
            # paging a never-seen agent contradicts "offline is normal".
            offline = last_seen is not None and (now - last_seen) >= offline_after
            prev_offline = st.get(("offline", aid), False)
            if offline and not prev_offline:
                try:
                    if await emit_agent_offline(
                        session,
                        agent_id=aid,
                        name=agent.name,
                        hostname=agent.hostname,
                        last_seen_at=last_seen,
                        offline_seconds=(now - last_seen).total_seconds(),
                        status="offline",
                        now=now,
                    ):
                        result["offline"] += 1
                except Exception:  # noqa: BLE001 - alert must not break monitor
                    log.warning("agent-offline alert emit failed for %s", aid, exc_info=True)
                    await session.rollback()
                log.warning(
                    "agent %s (%s) offline since %s", agent.name, aid, last_seen
                )
            elif prev_offline and not offline:
                try:
                    if await emit_agent_offline(
                        session,
                        agent_id=aid,
                        name=agent.name,
                        hostname=agent.hostname,
                        last_seen_at=last_seen,
                        offline_seconds=0.0,
                        status="recovered",
                        now=now,
                    ):
                        result["offline_recovered"] += 1
                except Exception:  # noqa: BLE001
                    log.warning("agent-recovery alert emit failed for %s", aid, exc_info=True)
                    await session.rollback()
                log.info("agent %s (%s) back online", agent.name, aid)
            st[("offline", aid)] = offline

            # --- replication stall (only when alive) ----------------------
            if offline:
                # Offline is already signalled (softer). Do not double-alert; leave
                # the stall state as-is so it re-evaluates cleanly on reappearance.
                continue

            newest_applied = (
                await session.execute(
                    select(AgentReplicationLog.applied_at)
                    .where(AgentReplicationLog.agent_id == agent.id)
                    .order_by(AgentReplicationLog.seq_no.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            # Fresh-enrollee guard: never replicated AND never reconciled => it
            # cannot have "stalled". Clear any stale state and skip.
            if newest_applied is None and agent.last_reconcile_at is None:
                st[("stall", aid)] = False
                continue

            candidates = [
                t for t in (newest_applied, agent.last_reconcile_at) if t is not None
            ]
            watermark = max(candidates)
            stalled = (now - watermark) >= stall_after
            prev_stalled = st.get(("stall", aid), False)
            if stalled and not prev_stalled:
                try:
                    if await emit_agent_replication_stall(
                        session,
                        agent_id=aid,
                        name=agent.name,
                        hostname=agent.hostname,
                        last_applied_at=newest_applied,
                        watermark=watermark,
                        stall_seconds=(now - watermark).total_seconds(),
                        status="stalled",
                        now=now,
                    ):
                        result["stalled"] += 1
                except Exception:  # noqa: BLE001
                    log.warning("agent-stall alert emit failed for %s", aid, exc_info=True)
                    await session.rollback()
                log.warning(
                    "agent %s (%s) alive but replication stalled since %s",
                    agent.name, aid, watermark,
                )
            elif prev_stalled and not stalled:
                try:
                    if await emit_agent_replication_stall(
                        session,
                        agent_id=aid,
                        name=agent.name,
                        hostname=agent.hostname,
                        last_applied_at=newest_applied,
                        watermark=watermark,
                        stall_seconds=0.0,
                        status="recovered",
                        now=now,
                    ):
                        result["stall_recovered"] += 1
                except Exception:  # noqa: BLE001
                    log.warning("agent-stall recovery emit failed for %s", aid, exc_info=True)
                    await session.rollback()
                log.info("agent %s (%s) replication resumed", agent.name, aid)
            st[("stall", aid)] = stalled

    return result


@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.tasks.agentmon.monitor_agents",
    queueing_lock="monitor-agents",  # FIX-8/FIX-9: no retry (5-minutely re-runs)
)
async def monitor_agents(timestamp: int) -> dict:
    """Maintenance tick: fire agent-offline / replication-stall ops alerts (P8-T11).

    No-op when ``agents_enabled`` is false (a single-node deploy is untouched).
    Returns the ``run_agent_monitor`` counters."""
    settings = get_settings()
    if not settings.agents_enabled:
        return {
            "skipped": "agents disabled",
            "evaluated": 0,
            "offline": 0,
            "offline_recovered": 0,
            "stalled": 0,
            "stall_recovered": 0,
        }
    return await run_agent_monitor(now=datetime.fromtimestamp(timestamp, tz=UTC))
