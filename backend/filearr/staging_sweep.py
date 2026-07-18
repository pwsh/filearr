"""P10-T8 — staging TTL cleanup sweep (central staging-disk bound).

The maintenance-tick reaper for ``staging_transfers`` rows/files
(worker.cleanup_staging_transfers drives it every 5 minutes). It moves a dead
transfer to the ``expired`` terminal state (via ``transfers.transfer_state_machine``),
deletes its staged file from ``FILEARR_STAGING_DIR``, and expires the underlying
``stage_upload`` command (via ``agentsync.command_state_machine``) so a lingering
agent stops working on a transfer nobody can download — mirroring the existing
purge/reconcile sweeps (bounded, idempotent, logs its counts).

Two independent reclaim schedules (research §5 / task P10-T8):

  (a) **TTL-expired** — any non-terminal row past its ``expires_at`` is reaped,
      EXCEPT one being ACTIVELY downloaded: a staged row whose
      ``last_range_request_at`` is within ``download_grace_seconds`` of now is a
      slow client still draining bytes, so it is left untouched (never cut a
      download mid-stream). Once the download stops and the grace lapses, the
      next sweep reclaims it.

  (b) **Abandoned partial upload** — a row still in ``pending``/``uploading`` that
      has made NO progress for ``abandoned_upload_seconds`` (measured from
      ``updated_at``, bumped on every PATCH append) is reclaimed on this SHORTER
      schedule even before its (longer) attach TTL: the agent died mid-upload and
      is not resuming, so its staged prefix should not squat on disk for the full
      window. A partial upload is never a download, so the active-download guard
      does not apply to it.

Pure w.r.t. the DB session it is handed (the caller owns the transaction and
commits); the only side effect beyond row mutation is the best-effort file
unlink (a missing/undeletable file never fails the sweep — the row still expires,
which is the disk-accounting truth we care about)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, transfers
from filearr.models import AgentCommand, StagingTransfer

logger = logging.getLogger("filearr.staging_sweep")

#: Non-terminal states the sweep ever touches.
_LIVE_STATES = ("pending", "uploading", "staged")
#: Partial-upload states eligible for the abandoned-progress reclaim.
_PARTIAL_STATES = ("pending", "uploading")
#: Bound the per-tick fanout so a large backlog is reclaimed incrementally
#: (mirrors the other bounded sweeps); the next tick picks up the remainder.
_SWEEP_BATCH = 1000


async def run_staging_cleanup_sweep(
    session: AsyncSession,
    *,
    now: datetime,
    download_grace_seconds: int,
    abandoned_upload_seconds: int,
) -> dict:
    """Reap dead ``staging_transfers`` (P10-T8). See the module docstring for the
    two reclaim schedules and the active-download carve-out. Commits the
    transaction. Returns
    ``{reaped, ttl_expired, abandoned, skipped_active, commands_expired}``."""
    grace_cutoff = now - timedelta(seconds=download_grace_seconds)
    abandoned_cutoff = now - timedelta(seconds=abandoned_upload_seconds)

    candidates = list(
        (
            await session.execute(
                select(StagingTransfer)
                .where(
                    StagingTransfer.state.in_(_LIVE_STATES),
                    or_(
                        StagingTransfer.expires_at <= now,
                        and_(
                            StagingTransfer.state.in_(_PARTIAL_STATES),
                            StagingTransfer.updated_at <= abandoned_cutoff,
                        ),
                    ),
                )
                .order_by(StagingTransfer.expires_at)
                .limit(_SWEEP_BATCH)
            )
        ).scalars()
    )

    reaped = ttl_expired = abandoned = skipped_active = commands_expired = 0
    for t in candidates:
        is_ttl = t.expires_at <= now
        is_abandoned = (
            t.state in _PARTIAL_STATES and t.updated_at <= abandoned_cutoff
        )
        # Active-download carve-out applies only to the TTL path (an abandoned
        # partial is never a download). A staged row still being drained within
        # the grace window is left for the next sweep.
        if is_ttl and not is_abandoned:
            if (
                t.last_range_request_at is not None
                and t.last_range_request_at > grace_cutoff
            ):
                skipped_active += 1
                continue

        # Reclaim: terminal state (via the frozen machine) + drop the staged file.
        t.state = transfers.transfer_state_machine(t.state, "expire")
        if t.staged_path:
            try:
                os.remove(t.staged_path)
            except OSError:  # best-effort — the row still expires
                pass
        # Expire the underlying stage_upload command so the agent stops working on
        # a dead transfer (skip if already terminal — a completed/cancelled one).
        cmd = await session.get(AgentCommand, t.command_id)
        if cmd is not None and not agentsync.command_is_terminal(cmd.status):
            cmd.status = agentsync.command_state_machine(cmd.status, "expire")
            cmd.completed_at = now
            cmd.updated_at = now
            commands_expired += 1

        reaped += 1
        if is_abandoned and not is_ttl:
            abandoned += 1
        else:
            ttl_expired += 1

    if reaped or skipped_active:
        await session.commit()
        logger.info(
            "staging cleanup: reaped=%d (ttl=%d abandoned=%d) commands_expired=%d "
            "skipped_active=%d",
            reaped,
            ttl_expired,
            abandoned,
            commands_expired,
            skipped_active,
        )
    return {
        "reaped": reaped,
        "ttl_expired": ttl_expired,
        "abandoned": abandoned,
        "skipped_active": skipped_active,
        "commands_expired": commands_expired,
    }
