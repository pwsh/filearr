"""P10-T3 — agent stat_check / rehash_check verification reconcile (central-side).

When an agent completes a ``stat_check`` / ``rehash_check`` :class:`AgentCommand`
(``filearr.api.agent_commands.complete_command``), central reconciles the reported
:class:`filearr.transfers.CommandResult` against the catalog row for the command's
item. This module owns that pure-ish reconcile step and its follow-up (alert +
index projection), kept out of the endpoint so both are unit-testable in isolation.

Reconcile matrix (the P10-T3 acceptance set):

* **deleted** (``exists=False``) — tombstone the item ``active → missing``
  (invariant 4: never hard-delete) and fire the mismatch alert. A re-verify of an
  already-missing item is an idempotent no-op confirm (no alert, no projection
  churn). ``last_verified_at`` is stamped regardless.
* **changed** (``exists=True`` + size/mtime drift, or a previously-missing item
  found present again) — update the drifted fields, stamp ``last_verified_at``,
  fire the alert.
* **hash mismatch** (``rehash_check`` only; the reported ``quick_hash`` /
  ``content_hash`` disagree) — correct the stored hash, fire the alert. This is
  the whole point of a rehash: silent byte-level corruption a size/mtime check
  cannot see.
* **content_skipped** — a ``content_hash=None`` on a rehash where ``content`` was
  requested but skipped (missing / oversize) is NOT a mismatch: the guard only
  ever compares a reported hash that is present, so a skipped hash never clears
  or falsely re-flags the stored one.
* **unchanged** — nothing differs: only ``last_verified_at`` is stamped, no alert,
  no re-index (``last_verified_at`` is not part of the Meili projection).

Invariant 5 is honoured by the caller: the item mutation commits in the same
transaction as the command's terminal status, then :func:`finalize_completion`
defers the ``index_sync`` AFTER that commit. The alert reuses the P8 seeded-system-
rule machinery (``filearr.alerts.ops.emit_agent_verify_mismatch``) and is wrapped
so an alert-layer fault can never fail the agent's completion request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from filearr.models import AgentCommand, Item, ItemStatus
from filearr.transfers import CommandResult

log = logging.getLogger("filearr.verify")

#: The command kinds this module reconciles. ``stage_upload`` is the retrieve
#: trigger (P10-T4), not a verification, and is left alone.
VERIFY_KINDS: frozenset[str] = frozenset({"stat_check", "rehash_check"})

#: mtime comparison tolerance (seconds). The agent reports mtime as float epoch
#: seconds and central stores a ``timestamptz``; a whole-second window absorbs
#: float/precision jitter of a byte-identical file while still catching a real
#: edit (which moves mtime by seconds).
MTIME_EPSILON = 1.0


@dataclass
class VerifyOutcome:
    """The result of reconciling one verify completion (see module docstring).

    ``mismatch`` is ``None`` (unchanged / idempotent confirm), ``"deleted"`` (the
    file is gone), or ``"changed"`` (size/mtime/hash drift or a reappearance).
    ``differed`` names the drifted fields for the alert payload.
    ``mutated_projection`` is True when a Meili-projected column changed (status /
    size / mtime / hash) and the item must be re-indexed AFTER commit.
    ``tombstoned`` is True only on the ``active → missing`` transition."""

    item_id: str
    library_id: object | None
    rel_path: str
    mismatch: str | None = None
    differed: list[str] = field(default_factory=list)
    mutated_projection: bool = False
    tombstoned: bool = False


def _parse_result(result: dict) -> CommandResult | None:
    """Validate the agent-reported result against the frozen contract, or None."""
    try:
        return CommandResult.model_validate(result)
    except Exception:  # noqa: BLE001 — a malformed result must not fail completion
        log.warning("verify: unparseable command result; skipping reconcile")
        return None


async def reconcile_completion(
    session, cmd: AgentCommand, result: dict, *, now: datetime | None = None
) -> VerifyOutcome | None:
    """Reconcile a completed verify command's result against its item.

    Mutates the item in ``session`` (no commit — the caller commits it atomically
    with the command's terminal status). Returns a :class:`VerifyOutcome`, or
    ``None`` when there is nothing to reconcile (unparseable result / vanished
    item) — the command still completes; only the reconcile is skipped."""
    now = now or datetime.now(UTC)
    if cmd.kind not in VERIFY_KINDS:
        return None
    parsed = _parse_result(result)
    if parsed is None:
        return None
    item = await session.get(Item, cmd.item_id)
    if item is None:
        return None

    outcome = VerifyOutcome(
        item_id=str(item.id), library_id=item.library_id, rel_path=item.rel_path
    )

    if not parsed.exists:
        # Deleted: tombstone active → missing (invariant 4). An already-missing
        # item is an idempotent confirm (no alert, no re-index).
        if item.status == ItemStatus.active:
            item.status = ItemStatus.missing
            outcome.mismatch = "deleted"
            outcome.differed = ["exists"]
            outcome.tombstoned = True
            outcome.mutated_projection = True
        item.last_verified_at = now
        return outcome

    # exists == True.
    differed: list[str] = []
    mutated = False

    # A previously-tombstoned item the agent now reports present again: reactivate
    # (the catalog was stale) and treat as a change worth surfacing.
    if item.status == ItemStatus.missing:
        item.status = ItemStatus.active
        differed.append("restored")
        mutated = True

    if parsed.size is not None and parsed.size != item.size:
        item.size = parsed.size
        differed.append("size")
        mutated = True

    if parsed.mtime is not None and _mtime_differs(item.mtime, parsed.mtime):
        item.mtime = datetime.fromtimestamp(parsed.mtime, UTC)
        differed.append("mtime")
        mutated = True

    # Hash comparison (rehash_check). Only ever compare a REPORTED (non-None) hash
    # — a skipped content hash (content_skipped) is None and must never be read as
    # a mismatch that clears the stored one.
    if parsed.quick_hash is not None and parsed.quick_hash != item.quick_hash:
        item.quick_hash = parsed.quick_hash
        differed.append("quick_hash")
        mutated = True
    if parsed.content_hash is not None and parsed.content_hash != item.content_hash:
        item.content_hash = parsed.content_hash
        differed.append("content_hash")
        mutated = True

    item.last_verified_at = now
    if differed:
        outcome.mismatch = "changed"
        outcome.differed = differed
        outcome.mutated_projection = mutated
    return outcome


def _mtime_differs(stored: datetime | None, reported: float) -> bool:
    """True when the reported float-epoch mtime is more than ``MTIME_EPSILON``
    seconds from the stored value (a real edit, not float jitter)."""
    if stored is None:
        return True
    return abs(stored.timestamp() - reported) >= MTIME_EPSILON


async def finalize_completion(
    session, agent, outcome: VerifyOutcome, *, now: datetime | None = None
) -> None:
    """Post-commit follow-up for a reconciled verify completion.

    Fires the ``System: agent verification mismatch`` alert (when the seeded rule
    is enabled) and defers the item's ``index_sync`` — BOTH wrapped so an alert- or
    queue-layer fault can never fail the agent's completion request. Must be called
    AFTER the item mutation has committed (invariant 5: the index projection is
    deferred only once the row is durable)."""
    now = now or datetime.now(UTC)
    if outcome.mismatch is not None:
        try:
            from filearr.alerts.ops import emit_agent_verify_mismatch

            await emit_agent_verify_mismatch(
                session,
                item_id=outcome.item_id,
                library_id=outcome.library_id,
                rel_path=outcome.rel_path,
                agent_id=str(agent.id),
                agent_name=agent.name,
                mismatch=outcome.mismatch,
                differed=outcome.differed,
                now=now,
            )
        except Exception:  # noqa: BLE001 — alert must not break command completion
            log.warning(
                "verify: mismatch alert emit failed for item %s",
                outcome.item_id,
                exc_info=True,
            )
            await session.rollback()
    if outcome.mutated_projection:
        try:
            from filearr.worker import defer_index_sync

            await defer_index_sync([outcome.item_id])  # invariant 5: AFTER commit
        except Exception:  # noqa: BLE001 — index is disposable/rebuildable
            log.warning(
                "verify: index_sync defer failed for item %s",
                outcome.item_id,
                exc_info=True,
            )
