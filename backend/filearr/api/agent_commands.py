"""P10-T1 — the ``agent_commands`` on-demand command primitive (central-side).

The queue through which central asks an agent to do one thing — ``stat_check`` /
``rehash_check`` (existence/freshness) or ``stage_upload`` (retrieve trigger) —
distinct from Phase-5's policy/replication channels (research §3.1, osquery
``distributed_interval`` precedent). This module is the *central* surface only:
the durable table, its lifecycle, and two auth planes. The agent runtime that
long-polls this queue is P5-T4; the retrieve data plane that consumes a
``stage_upload`` is P10-T4/T6/T13.

Two auth planes, both behind the ``FILEARR_AGENTS_ENABLED`` gate (404 when off,
same as the enrollment surface):

* **Operator/admin plane** — enqueue (``write``), list / get (``read``), cancel
  (``write``). RBAC ``download``-gating of creation is Wave 4 (P6-T4 / R2); the
  coarse ``write`` scope is the stand-in today, exactly as the transfer endpoints
  do. Enqueue + cancel emit ``security_events``; per-poll churn does NOT (noise).
* **Agent plane** — poll / ack / complete. Authenticated with the P5-T1 INTERIM
  agent-plane credential (the agent's bound ``cert_fingerprint`` as a bearer
  token — the only durable per-agent secret before mTLS). **mTLS replaces this in
  P5-T6**: the request identity becomes the verified client cert and this bearer
  check is removed. Documented interim, gated off by default.

Lifecycle + the TTL/redelivery sweep live in :mod:`filearr.agentsync`
(``command_state_machine`` / ``run_agent_command_sweep``); the periodic wrapper
is :func:`filearr.worker.expire_agent_commands`.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, audit, verify
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, Item
from filearr.security import require_scope
from filearr.worker import defer_index_sync

router = APIRouter()

CommandKind = Literal["stat_check", "rehash_check", "stage_upload"]


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class CommandEnqueueIn(BaseModel):
    kind: CommandKind
    item_id: uuid.UUID
    payload: dict[str, Any] = Field(default_factory=dict)
    # Optional per-command TTL override (seconds); clamped server-side.
    ttl_seconds: int | None = Field(default=None, ge=60)


class CommandOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    kind: str
    item_id: uuid.UUID
    payload: dict[str, Any]
    status: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    picked_up_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    requested_by: uuid.UUID | None

    @classmethod
    def of(cls, c: AgentCommand) -> CommandOut:
        return cls(
            id=c.id,
            agent_id=c.agent_id,
            kind=c.kind,
            item_id=c.item_id,
            payload=c.payload or {},
            status=c.status,
            attempts=c.attempts,
            created_at=c.created_at,
            updated_at=c.updated_at,
            expires_at=c.expires_at,
            picked_up_at=c.picked_up_at,
            completed_at=c.completed_at,
            result=c.result,
            requested_by=c.requested_by,
        )


class PollIn(BaseModel):
    # How many commands to drain in one poll. Clamped to FILEARR_AGENT_COMMAND_POLL_MAX.
    # NOTE (P5-T4): this is a PLAIN poll — no server-side long-poll / hold-open
    # yet. The held-open long-poll rides P5-T4's poll/ETag machinery.
    max: int = Field(default=10, ge=1)


class CompleteIn(BaseModel):
    ok: bool = True
    result: dict[str, Any] | None = None


def _json_len(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Agent-plane auth (P5-T6: mTLS-header modes supersede the interim bearer)      #
# --------------------------------------------------------------------------- #
# Headers the Caddy ``agents.<domain>`` mTLS site stamps after it has VERIFIED
# the client cert against the step-ca root (require_and_verify). They are only
# trusted when X-Filearr-Proxy-Auth matches ``FILEARR_PROXY_SHARED_SECRET`` —
# i.e. the request demonstrably transited our own proxy, never a direct hit.
_HDR_PROXY_AUTH = "x-filearr-proxy-auth"  # shared secret (trust gate)
_HDR_AGENT_SAN = "x-filearr-agent-san"    # client cert first DNS SAN == agent_id
_HDR_AGENT_FP = "x-filearr-agent-fp"      # client cert fingerprint (secondary)


async def _authenticate_agent(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """Authenticate an agent-plane request per ``FILEARR_AGENT_AUTH_MODE``.

    * ``fingerprint`` (default) — the INTERIM P5-T1 scheme: the agent's bound
      ``cert_fingerprint`` as a bearer token.
    * ``mtls-header`` — trust ONLY the Caddy-forwarded, already-verified mTLS
      identity (SAN == agent_id), gated by the proxy shared secret; the bearer
      is refused.
    * ``both`` — mtls-header when the proxy-auth header is present (hard-fails on
      a bad secret/SAN), else the bearer path (migration window).

    401 for a missing/mismatched credential, 404 for an unknown agent, 403 for a
    revoked or still-pending (unbound) agent."""
    mode = get_settings().agent_auth_mode
    has_proxy_header = request.headers.get(_HDR_PROXY_AUTH) is not None
    if mode == "mtls-header" or (mode == "both" and has_proxy_header):
        return await _authenticate_agent_mtls(session, agent_id, request)
    return await _authenticate_agent_bearer(session, agent_id, request)


async def _authenticate_agent_bearer(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """INTERIM agent-plane auth (P5-T1): the agent presents its bound
    ``cert_fingerprint`` as a bearer token — the only durable per-agent secret
    before mTLS.

    401 for a missing/mismatched bearer, 404 for an unknown agent, 403 for a
    revoked or still-pending (unbound) agent — a pending agent has no fingerprint
    and so cannot use this plane at all."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "agent credential required")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent revoked")
    if not agent.cert_fingerprint:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent not active")
    if not secrets.compare_digest(token, agent.cert_fingerprint):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid agent credential")
    return agent


async def _authenticate_agent_mtls(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """P5-T6 mTLS-header auth: trust the Caddy-forwarded, already-verified client
    identity when (and only when) X-Filearr-Proxy-Auth matches the configured
    shared secret. Identity is the client cert's DNS SAN, which the enroll flow
    sets to ``str(agent_id)`` — renewal-PROOF (the SAN survives cert rotation, so
    the interim fingerprint-drift caveat does not apply). The bearer token is NOT
    consulted here — the weaker path is shut off in this mode.

    401 for a missing/wrong shared secret (bearer alone can't authenticate),
    403 for a SAN that does not match the path agent_id or a fingerprint header
    that contradicts the bound one, 404 for an unknown agent, 403 for a revoked
    agent."""
    secret = get_settings().proxy_shared_secret or ""
    provided = request.headers.get(_HDR_PROXY_AUTH) or ""
    # Fail closed when the secret is unconfigured: an empty configured secret must
    # never authenticate (else the whole plane is open).
    if not secret or not provided or not secrets.compare_digest(provided, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "proxy authentication required")
    san = request.headers.get(_HDR_AGENT_SAN) or ""
    if not san or san != str(agent_id):
        # A valid mTLS cert, but for a different agent than the URL path — the
        # caller is authenticated as someone else (authorization failure).
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent identity mismatch")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent revoked")
    # Secondary defence-in-depth: when the agent row has a bound fingerprint AND
    # the proxy forwarded one, they must agree. Skipped when either is absent so a
    # freshly-renewed leaf (new fingerprint, same SAN) is not locked out — SAN is
    # the authoritative identity.
    fp = request.headers.get(_HDR_AGENT_FP) or ""
    if agent.cert_fingerprint and fp and not secrets.compare_digest(fp, agent.cert_fingerprint):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent fingerprint mismatch")
    return agent


async def _owned_command(
    session: AsyncSession, agent: Agent, command_id: uuid.UUID
) -> AgentCommand:
    """Load a command that MUST belong to ``agent`` — a wrong-agent id is a 404
    (never leak another agent's command existence)."""
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None or cmd.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    return cmd


# --------------------------------------------------------------------------- #
# Operator/admin plane — enqueue / list / get / cancel                         #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/commands",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def enqueue_command(
    agent_id: uuid.UUID,
    body: CommandEnqueueIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Enqueue one command for an agent (P10-T1).

    ``write`` scope is the coarse gate today; **Wave 4 (P6-T4 / R2)** additionally
    evaluates the path-scoped RBAC ``download`` action BEFORE the row is created
    (``stat_check`` needs only ``search_metadata``; ``rehash_check`` /
    ``stage_upload`` need ``download``) — authorization stops the costly side
    effect, it does not clean up after it. Enqueue is audited unconditionally."""
    settings = get_settings()
    if _json_len(body.payload) > settings.agent_command_payload_max_bytes:
        raise HTTPException(
            413, "payload too large"
        )
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent revoked")
    item = (
        await session.execute(select(Item).where(Item.id == body.item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")

    ttl = body.ttl_seconds or settings.agent_command_ttl_seconds
    ttl = max(60, min(ttl, settings.agent_command_ttl_max_seconds))
    now = datetime.now(UTC)
    cmd = AgentCommand(
        agent_id=agent_id,
        kind=body.kind,
        item_id=body.item_id,
        payload=body.payload,
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=ttl),
        requested_by=_actor_uuid(request),
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": body.kind,
            "item_id": str(body.item_id),
            "ttl_seconds": ttl,
        },
    )
    return CommandOut.of(cmd)


@router.get(
    "/agent-commands",
    response_model=list[CommandOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def list_commands(
    session: AsyncSession = Depends(get_session),
    agent_id: uuid.UUID | None = None,
    state: str | None = None,
    kind: str | None = None,
    before: uuid.UUID | None = None,
    limit: int = 50,
) -> list[CommandOut]:
    """List commands newest-first with keyset pagination (``before`` = the last id
    of the previous page; ``id`` is uuidv7 → time-ordered). Filter by ``agent_id``
    / ``state`` / ``kind``."""
    limit = max(1, min(limit, 200))
    q = select(AgentCommand).order_by(AgentCommand.id.desc()).limit(limit)
    if agent_id is not None:
        q = q.where(AgentCommand.agent_id == agent_id)
    if state is not None:
        q = q.where(AgentCommand.status == state)
    if kind is not None:
        q = q.where(AgentCommand.kind == kind)
    if before is not None:
        q = q.where(AgentCommand.id < before)
    rows = (await session.execute(q)).scalars().all()
    return [CommandOut.of(c) for c in rows]


@router.get(
    "/agent-commands/{command_id}",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def get_command(
    command_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> CommandOut:
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    return CommandOut.of(cmd)


@router.post(
    "/agent-commands/{command_id}/cancel",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def cancel_command(
    command_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Cancel a PRE-TERMINAL command (409 if already terminal). Audited."""
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    if agentsync.command_is_terminal(cmd.status):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command already {cmd.status}"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(cmd.status, "cancel")
    cmd.completed_at = now
    cmd.updated_at = now
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_CANCELLED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"command_id": str(command_id), "agent_id": str(cmd.agent_id)},
    )
    return CommandOut.of(cmd)


# --------------------------------------------------------------------------- #
# Agent plane — poll / ack / complete (interim bearer; mTLS in P5-T6)          #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/commands/poll",
    response_model=list[CommandOut],
    dependencies=[Depends(require_agents_enabled)],
)
async def poll_commands(
    agent_id: uuid.UUID,
    body: PollIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[CommandOut]:
    """Drain up to ``max`` pending, not-yet-expired commands FIFO, delivering each
    (``pending`` → ``picked_up``; ``attempts`` incremented, lease clock started).
    ``FOR UPDATE SKIP LOCKED`` so concurrent polls/sweeps never block. A poll also
    refreshes ``agents.last_seen_at`` (the agent is demonstrably alive). Plain
    poll — no long-poll hold-open yet (P5-T4)."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    want = min(body.max, settings.agent_command_poll_max)
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(AgentCommand)
            .where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.status == "pending",
                AgentCommand.expires_at > now,
            )
            .order_by(AgentCommand.created_at.asc())
            .limit(want)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for cmd in rows:
        cmd.status = agentsync.command_state_machine(cmd.status, "deliver")
        cmd.picked_up_at = now
        cmd.attempts += 1
        cmd.updated_at = now
    agent.last_seen_at = now
    await session.commit()
    return [CommandOut.of(c) for c in rows]


@router.post(
    "/agents/{agent_id}/commands/{command_id}/ack",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def ack_command(
    agent_id: uuid.UUID,
    command_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Lease heartbeat for an in-flight (``picked_up``) command: refresh the lease
    clock so a genuinely-working slow command is not reclaimed by the redelivery
    sweep. 409 if the command is not in-flight (already terminal / not delivered)."""
    agent = await _authenticate_agent(session, agent_id, request)
    cmd = await _owned_command(session, agent, command_id)
    if cmd.status != "picked_up":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command not in-flight (is {cmd.status})"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(cmd.status, "ack")
    cmd.picked_up_at = now  # refresh lease
    cmd.updated_at = now
    await session.commit()
    return CommandOut.of(cmd)


@router.post(
    "/agents/{agent_id}/commands/{command_id}/complete",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def complete_command(
    agent_id: uuid.UUID,
    command_id: uuid.UUID,
    body: CompleteIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Report a terminal result for a picked-up command (``done`` / ``failed``).

    Idempotent replay: re-completing an already-``done`` row is a no-op that
    returns the stored result (mirrors replication's at-least-once posture). A
    different terminal state (``failed`` / ``expired`` / ``cancelled``) or a
    never-delivered (``pending``) command is a 409. Result size is capped."""
    agent = await _authenticate_agent(session, agent_id, request)
    cmd = await _owned_command(session, agent, command_id)
    if cmd.status == "done":
        return CommandOut.of(cmd)  # idempotent success replay
    if cmd.status != "picked_up":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command not in-flight (is {cmd.status})"
        )
    if body.result is not None and (
        _json_len(body.result) > get_settings().agent_command_result_max_bytes
    ):
        raise HTTPException(
            413, "result too large"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(
        cmd.status, "complete" if body.ok else "fail"
    )
    cmd.result = body.result
    cmd.completed_at = now
    cmd.updated_at = now
    # P10-T3: reconcile a successful stat_check/rehash_check against the item IN
    # THE SAME transaction as the terminal status (the item mutation + command
    # completion are atomic). The follow-up alert + index_sync run AFTER commit
    # (invariant 5). A failed/unparseable/non-verify completion reconciles nothing.
    outcome = None
    if body.ok and cmd.kind in verify.VERIFY_KINDS and body.result is not None:
        outcome = await verify.reconcile_completion(session, cmd, body.result, now=now)
    await session.commit()
    if outcome is not None:
        await verify.finalize_completion(session, agent, outcome, now=now)
    # P10-T9 (R2): a completed rehash_check reads the file's full CONTENT on the
    # agent — a data-access event audited UNCONDITIONALLY (regardless of
    # FILEARR_AUDIT_READS), mirroring the transfer-download carve-out. Fired on the
    # terminal completion (done OR failed) so the read attempt is always recorded;
    # an idempotent replay of an already-``done`` command returned above and never
    # re-audits. A stat_check is a metadata-only existence probe — not audited.
    # The actor is the agent (no principal), so agent_id is recorded in details.
    if cmd.kind == "rehash_check":
        await audit.emit(
            audit.AGENT_VERIFY_COMPLETED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "command_id": str(command_id),
                "agent_id": str(agent_id),
                "item_id": str(cmd.item_id),
                "kind": cmd.kind,
                "ok": body.ok,
                "mismatch": outcome.mismatch if outcome is not None else None,
                "differed": outcome.differed if outcome is not None else [],
            },
        )
    return CommandOut.of(cmd)


# --------------------------------------------------------------------------- #
# Agent plane — replication batch apply (P5-T4; interim bearer, mTLS in P5-T6)  #
# --------------------------------------------------------------------------- #
class ReplicationResult(BaseModel):
    """The apply outcome returned on a 200 (P5-T4). ``last_seq`` is the agent's
    new contiguous watermark (``agents.last_contiguous_seq_no``); ``noop_tombstones``
    is the R2 reconciliation metric (tombstones against already-purged rows)."""

    applied: int
    upserted: int
    tombstoned: int
    noop_tombstones: int
    libraries_created: int
    last_seq: int


@router.post(
    "/agents/{agent_id}/replication-batch",
    response_model=ReplicationResult,
    dependencies=[Depends(require_agents_enabled)],
)
async def apply_replication_batch(
    agent_id: uuid.UUID,
    body: agentsync.ReplicationBatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Apply one agent replication batch to central items + the replication ledger
    (P5-T4). Behind ``FILEARR_AGENTS_ENABLED`` (404 off), interim agent bearer
    auth (the bound ``cert_fingerprint``; mTLS replaces it in P5-T6).

    Flow: authenticate the agent → require the body ``agent_id`` to match the path
    (403 on mismatch) → cap on entries (413) → ``check_batch`` against
    ``agents.last_contiguous_seq_no``:

      * NOT a contiguous continuation → **409** ``{"reason", "expected_seq_no"}``
        (the frozen resend-from contract; the agent rewinds its outbox drain).
      * a clean continuation → :func:`agentsync.apply_batch` (one transaction:
        upserts, then tombstones, then the ledger + seq-watermark advance) → 200.

    The Meili projection for the touched item ids is deferred AFTER the commit
    (invariant 5). A poll-style ``last_seen_at`` refresh happens on both the 409
    and the applied path (the agent is demonstrably alive)."""
    agent = await _authenticate_agent(session, agent_id, request)
    # Never apply one agent's outbox under another's identity.
    if str(body.agent_id) != str(agent_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "batch agent_id mismatch")
    settings = get_settings()
    if len(body.entries) > settings.agent_replication_max_entries:
        raise HTTPException(413, "replication batch too large")
    verdict = agentsync.check_batch(body, agent.last_contiguous_seq_no)
    if not verdict.ok:
        # Resend-request: refresh liveness, hand back the seq to rewind to.
        agent.last_seen_at = datetime.now(UTC)
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reason": verdict.reason,
                "expected_seq_no": verdict.expected_seq_no,
            },
        )
    # apply_batch commits (items + ledger + last_contiguous_seq_no + last_seen).
    result = await agentsync.apply_batch(session, agent, body)
    item_ids = result.pop("item_ids", [])
    if item_ids:
        await defer_index_sync(item_ids)  # invariant 5: AFTER commit
    return ReplicationResult(**result)


# --------------------------------------------------------------------------- #
# Agent plane — full-manifest reconciliation sweep (P5-T5; interim bearer)      #
# --------------------------------------------------------------------------- #
class ReconcileStartIn(BaseModel):
    library_ref: str
    digest: str
    row_count: int = Field(ge=0)
    rebuilt: bool = False


class ReconcileStartOut(BaseModel):
    status: str  # "match" | "mismatch"
    session_id: str | None = None


class ReconcileRow(BaseModel):
    rel_path: str
    size: int
    mtime: float
    quick_hash: str | None = None
    content_hash: str | None = None


class ReconcileRowsIn(BaseModel):
    rows: list[ReconcileRow] = Field(default_factory=list)


class ReconcileRowsOut(BaseModel):
    staged: int


class ReconcileFinishIn(BaseModel):
    digest: str
    row_count: int = Field(ge=0)
    reset_seq: bool = False


class ReconcileResult(BaseModel):
    """The anti-join outcome on a 200 finish (P5-T5, ruling 3)."""

    status: str
    upserted: int
    tombstoned: int
    reactivated: int
    updated: int
    trashed_conflicts: int
    unchanged: int


_RECONCILE_COUNTERS = (
    "upserted",
    "tombstoned",
    "reactivated",
    "updated",
    "trashed_conflicts",
    "unchanged",
)


@router.post(
    "/agents/{agent_id}/reconcile/start",
    response_model=ReconcileStartOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_start_endpoint(
    agent_id: uuid.UUID,
    body: ReconcileStartIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReconcileStartOut:
    """Phase 1 of the full-manifest sweep (P5-T5). Compare the agent's whole-
    library digest to central's projection. Equal → ``match`` (watermark stamped;
    ``rebuilt`` resets the seq watermark). Otherwise open ONE live session for the
    agent (superseding any prior unfinished one) and return its ``session_id``.
    Interim agent bearer auth; behind ``FILEARR_AGENTS_ENABLED``."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    result = await agentsync.reconcile_start(
        session,
        agent,
        library_ref=body.library_ref,
        digest=body.digest,
        row_count=body.row_count,
        rebuilt=body.rebuilt,
        now=datetime.now(UTC),
        ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
    )
    if result["status"] == "match":
        await audit.emit(
            audit.AGENT_RECONCILED,
            request=request,
            principal_id=audit.actor_id(request),
            details={"agent_id": str(agent_id), "status": "match"},
        )
    return ReconcileStartOut(**result)


@router.post(
    "/agents/{agent_id}/reconcile/{session_id}/rows",
    response_model=ReconcileRowsOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_rows_endpoint(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ReconcileRowsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReconcileRowsOut:
    """Phase 2: page the agent's full manifest into staging (413 above
    ``FILEARR_AGENT_RECONCILE_PAGE_MAX``; 404 for an unknown/expired session). A
    re-sent page upserts (idempotent). Returns the running staged-row count."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    if len(body.rows) > settings.agent_reconcile_page_max:
        raise HTTPException(413, "reconcile page too large")
    try:
        staged = await agentsync.reconcile_stage_rows(
            session,
            agent,
            session_id=session_id,
            rows=body.rows,
            now=datetime.now(UTC),
            ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
        )
    except agentsync.ReconcileError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    return ReconcileRowsOut(staged=staged)


@router.post(
    "/agents/{agent_id}/reconcile/{session_id}/finish",
    response_model=ReconcileResult,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_finish_endpoint(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ReconcileFinishIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Phase 3: verify the staged manifest, run the anti-join in ONE transaction,
    stamp the watermark, drop the session. A staged digest/count that disagrees
    with the body → 409 ``{"reason":"digest_mismatch"}`` and the session is
    destroyed (the agent re-sweeps). 404 for an unknown/expired session. The Meili
    projection for touched ids is deferred AFTER the commit (invariant 5)."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    try:
        result = await agentsync.reconcile_finish(
            session,
            agent,
            session_id=session_id,
            digest=body.digest,
            row_count=body.row_count,
            reset_seq=body.reset_seq,
            now=datetime.now(UTC),
            ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
        )
    except agentsync.ReconcileError as err:
        if err.reason == "digest_mismatch":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"reason": "digest_mismatch"},
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    item_ids = result.pop("item_ids", [])
    if item_ids:
        await defer_index_sync(item_ids)  # invariant 5: AFTER commit
    await audit.emit(
        audit.AGENT_RECONCILED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "agent_id": str(agent_id),
            **{k: result[k] for k in _RECONCILE_COUNTERS},
        },
    )
    return ReconcileResult(**result)


def _actor_uuid(request: Request) -> uuid.UUID | None:
    aid = audit.actor_id(request)
    return uuid.UUID(aid) if aid else None
