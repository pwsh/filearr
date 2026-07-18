"""P10-T13: RBAC-gated agent file-transfer API — LIVE.

Graduated from the inert 501 contract to the real retrieve control plane. The
four endpoints drive an agent→central retrieve end to end:

* ``POST /items/{id}/transfer`` — initiate: create the
  ``agent_commands(kind='stage_upload')`` + ``staging_transfers`` rows and return
  ``202`` + the transfer id. The agent long-polls the command, attaches the
  pre-created staging row (idempotent per ``command_id``), streams the bytes
  (P10-T4), and central verifies them on completion (P10-T5).
* ``GET /transfers/{id}`` — status (state + verified + progress + timestamps).
* ``GET /transfers/{id}/download`` — Range-capable stream of the VERIFIED staged
  file (``Content-Disposition`` attachment); flips ``staged → downloaded`` once a
  response covers the final byte; re-downloadable until TTL.
* ``DELETE /transfers/{id}`` — cancel: tombstone the command, expire the transfer,
  reclaim any staged bytes.

Authorization (P6-T4 fully landed — the "Wave 4 later" deferral the task text
predated is wired NOW):

* coarse scope gates stay: ``write`` on initiate/download/cancel (bytes leaving a
  machine is mutation-tier), ``read`` on status;
* initiate AND download additionally evaluate the path-scoped RBAC ``download``
  action against the item's ``path_scope`` (``PermissionContext.authorize_item``)
  BEFORE any row is created / any byte is served — authorization stops the costly
  side effect, it does not clean up after it (R2 / brief §4.1);
* initiate AND a completed download write an audit line **unconditionally**,
  regardless of ``FILEARR_AUDIT_READS`` (R2 / brief §4 download-audit carve-out).

SSE progress + the Svelte UI are P10-T6, out of scope here.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, audit, transfers
from filearr import db as db_mod
from filearr.api.agent_staging import staged_path
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, Item, Library, StagingTransfer
from filearr.security import (
    PermissionContext,
    _verify_credentials,
    require_permission,
    require_scope,
)
from filearr.transfers import TransferRequest

router = APIRouter()

#: Terminal transfer states — the SSE stream sends a final event then closes.
_TERMINAL_STATES = ("downloaded", "expired", "failed")

#: SSE DB-poll cadence (NOT the framework keepalive interval). Kept short so the
#: retrieve UI ticks live; mirrors the scans SSE poll.
_SSE_POLL_INTERVAL = 1.0

_bearer = HTTPBearer(auto_error=False)

#: Transfer states from which a NEW retrieve may not be initiated (an active
#: transfer already owns the item's staging slot).
_ACTIVE_STATES = ("pending", "uploading", "staged")


async def _load_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    """Load the item (404 if it does not exist). Runs *after* scope + body
    validation but *before* the RBAC check + side effects."""
    item = (
        await session.execute(select(Item).where(Item.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


async def _load_transfer(session: AsyncSession, transfer_id: uuid.UUID) -> StagingTransfer:
    t = await session.get(StagingTransfer, transfer_id)
    if t is None:
        raise HTTPException(404, "Transfer not found")
    return t


def _actor_uuid(request: Request) -> uuid.UUID | None:
    aid = audit.actor_id(request)
    return uuid.UUID(aid) if aid else None


def _status_payload(t: StagingTransfer) -> dict:
    """The transfer status contract (brief §6): state + verified + progress +
    timestamps. ``bytes_transferred``/``total_bytes`` drive the progress bar."""
    return {
        "transfer_id": str(t.id),
        "item_id": str(t.item_id),
        "agent_id": str(t.agent_id),
        "state": t.state,
        "verified": t.verified,
        "bytes_transferred": t.bytes_transferred,
        "total_bytes": t.total_bytes,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_range_request_at": (
            t.last_range_request_at.isoformat() if t.last_range_request_at else None
        ),
    }


def _staged_stat(path: str | None) -> tuple[bool, int]:
    """``(exists, size)`` for the staged file. A sync helper (the FS stat is
    blocking, like FileResponse's own read) so it stays off the async call graph."""
    if not path:
        return False, 0
    try:
        return True, os.path.getsize(path)
    except OSError:
        return False, 0


def _range_covers_final_byte(range_header: str | None, size: int) -> bool:
    """True when a GET with this ``Range`` header yields a response that includes
    the file's LAST byte (so ``staged → downloaded`` is the honest transition).

    No ``Range`` header → a full-body GET → covers the final byte. A well-formed
    ``bytes=`` range whose last member reaches ``size-1`` (an open-ended
    ``start-`` or suffix ``-N``, or an explicit end ``>= size-1``) also covers it.
    Anything malformed or partial → ``False`` (conservative: a partial/early Range
    request must NOT flip the transfer to a terminal ``downloaded``)."""
    if not range_header:
        return True
    if size == 0:
        return True
    try:
        units, _, spec = range_header.partition("=")
        if units.strip().lower() != "bytes" or not spec:
            return False
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            start_s, _, end_s = part.partition("-")
            if start_s == "":  # suffix range 'bytes=-N' → always includes the tail
                return True
            if end_s == "":  # open-ended 'bytes=start-' → includes the tail
                return True
            if int(end_s) >= size - 1:
                return True
    except (ValueError, TypeError):
        return False
    return False


@router.post("/items/{item_id}/transfer", status_code=202)
async def initiate_transfer(
    item_id: uuid.UUID,
    body: TransferRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("download", coarse="write")),
) -> dict:
    """Initiate an agent→central retrieve (brief §5/§6).

    Order (authorization gates the costly side effect, R2/§4.1): 404 unknown item →
    RBAC ``download`` (404 outside read scope, 403 readable-but-denied) → 422 the
    item is not agent-hosted (a centrally-scanned item has local bytes) → 409 the
    hosting agent is revoked → 409 an active transfer already exists (its id is in
    the detail) → create the ``stage_upload`` command + the bound
    ``staging_transfers`` row → 202 + transfer id + state. Audited unconditionally."""
    item = await _load_item(session, item_id)
    ctx.authorize_item(item, action="download")

    library = await session.get(Library, item.library_id)
    agent_id = library.source_agent_id if library is not None else None
    if agent_id is None:
        raise HTTPException(
            422,
            "Item is not agent-hosted; a centrally-scanned item has local bytes and "
            "needs no transfer",
        )
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.revoked_at is not None:
        raise HTTPException(409, "Hosting agent is revoked")

    settings = get_settings()
    now = datetime.now(UTC)
    existing = (
        await session.execute(
            select(StagingTransfer)
            .where(
                StagingTransfer.item_id == item_id,
                StagingTransfer.state.in_(_ACTIVE_STATES),
                StagingTransfer.expires_at > now,
            )
            .order_by(StagingTransfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409, f"An active transfer already exists for this item: {existing.id}"
        )

    ttl = settings.transfer_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)
    cmd = AgentCommand(
        agent_id=agent_id,
        kind="stage_upload",
        item_id=item_id,
        # Keys the Go stage_upload executor consumes: library_ref == the agent-side
        # library root (Library.root_path, verbatim), rel_path == the item's rel
        # path. ``verify`` is the additive P10-T5 flag (ignored by the Go decoder,
        # which only reads library_ref/rel_path/content) driving hash verification
        # on staging completion.
        payload={
            "library_ref": library.root_path,
            "rel_path": item.rel_path,
            "verify": body.verify_hash,
        },
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        requested_by=_actor_uuid(request),
    )
    session.add(cmd)
    await session.flush()  # assign cmd.id for the transfer FK

    tid = uuid.uuid4()
    transfer = StagingTransfer(
        id=tid,
        item_id=item_id,
        agent_id=agent_id,
        command_id=cmd.id,
        # Initial lifecycle state per the transfer machine (pending → uploading →
        # staged → downloaded); the agent's first PATCH advances it (P10-T4).
        state="pending",
        bytes_transferred=0,
        total_bytes=item.size,
        staged_path=str(staged_path(settings, tid)),
        verified=False,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(transfer)
    try:
        await session.commit()
    except IntegrityError:
        # Race backstop (P10-T6): a concurrent initiate slipped past the
        # check-then-insert guard above and won the partial UNIQUE index
        # (uq_staging_transfers_active_item). Roll back, re-read the surviving
        # active transfer, and return the SAME 409-with-existing-id contract.
        await session.rollback()
        winner = (
            await session.execute(
                select(StagingTransfer)
                .where(
                    StagingTransfer.item_id == item_id,
                    StagingTransfer.state.in_(_ACTIVE_STATES),
                    StagingTransfer.expires_at > now,
                )
                .order_by(StagingTransfer.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        detail = (
            f"An active transfer already exists for this item: {winner.id}"
            if winner is not None
            else "An active transfer already exists for this item"
        )
        raise HTTPException(409, detail) from None

    await audit.emit(
        audit.AGENT_TRANSFER_INITIATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "transfer_id": str(tid),
            "command_id": str(cmd.id),
            "item_id": str(item_id),
            "agent_id": str(agent_id),
            "verify_hash": body.verify_hash,
        },
    )
    return {"transfer_id": str(tid), "state": transfer.state}


@router.get("/transfers/{transfer_id}", dependencies=[Depends(require_scope("read"))])
async def get_transfer(
    transfer_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Transfer status (brief §6): state + verified + progress + timestamps. 404
    for an unknown id. ``read`` scope."""
    t = await _load_transfer(session, transfer_id)
    return _status_payload(t)


# --------------------------------------------------------------------------- #
# P10-T6/T7 — SSE progress stream                                              #
# --------------------------------------------------------------------------- #
async def _require_transfer_events_scope(
    request: Request,
    api_key: str | None = None,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Auth for the transfer SSE stream (mirrors the scans SSE, scans.py).

    ``EventSource`` cannot set request headers, so a browser cannot send a Bearer
    token on the stream; we accept the key either via ``Authorization: Bearer``
    (non-browser clients) OR a ``?api_key=`` query param — ONLY on this read-only
    events endpoint. Verified through the same constant-time hash lookup + scope
    check as everything else (``read`` scope). No-op when auth is disabled."""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    token = creds.credentials if creds is not None else api_key
    if not token:
        raise HTTPException(401, "Missing bearer token or api_key")
    await _verify_credentials(token, "read", session, request)


def _waiting_for_agent(t: StagingTransfer, cmd: AgentCommand | None) -> bool:
    """The P10-T7 derived pseudo-state (NOT a DB state): the retrieve is
    ``pending`` AND the underlying ``stage_upload`` command has not been picked up
    yet — i.e. the hosting agent is offline / hasn't polled. This is the NORMAL
    case (an offline agent is expected, brief §5), surfaced so the UI shows a
    clear "waiting for agent" state rather than a spinner or an error."""
    return t.state == "pending" and cmd is not None and cmd.picked_up_at is None


def _terminal_reason(t: StagingTransfer, cmd: AgentCommand | None) -> str:
    """A machine-readable reason for a terminal transfer, driving UI messaging.

    ``downloaded`` / ``failed`` map to themselves. An ``expired`` transfer is
    disambiguated via its command: a ``cancelled`` command means the operator
    cancelled it; a command that was NEVER picked up (``picked_up_at is None``)
    means the agent stayed offline for the whole TTL — the P10-T7
    ``offline_timeout`` case, distinct from a mid-upload failure; anything else is
    an ordinary TTL/idle ``expired`` (e.g. the staged file's TTL lapsed after a
    completed upload)."""
    if t.state == "downloaded":
        return "downloaded"
    if t.state == "failed":
        return "failed"
    # expired
    if cmd is not None and cmd.status == "cancelled":
        return "cancelled"
    if cmd is None or cmd.picked_up_at is None:
        return "offline_timeout"
    return "expired"


@router.get("/transfers/{transfer_id}/events", response_class=EventSourceResponse)
async def transfer_events(
    transfer_id: uuid.UUID,
    _: None = Depends(_require_transfer_events_scope),
):
    """Native SSE stream of retrieve progress (P10-T6), mirroring the scans SSE
    (EventSourceResponse discipline: framework-driven keepalive ``: ping`` on
    idle, clean generator teardown on client disconnect).

    Events (all carry the status payload + the derived ``waiting_for_agent``):
      * ``progress`` — emitted on first read and whenever state/bytes/verified/
        waiting change. While ``waiting_for_agent`` is true the UI shows the
        offline-agent waiting state (P10-T7), not a spinner.
      * ``offline_timeout`` — a terminal frame when the transfer ``expired`` while
        the agent never picked the command up (P10-T7 lapsed-TTL failure). Emitted
        just before ``done``.
      * ``error`` — a terminal frame for a ``failed`` (integrity-verification)
        transfer, or ``{"detail": ...}`` for an unknown id.
      * ``done`` — the terminal snapshot (carries ``reason``); the stream closes.

    A short DB poll (``_SSE_POLL_INTERVAL``) re-reads the row + its command each
    tick; a terminal state emits its final frame(s) and returns."""
    last_key: str | None = None
    while True:
        async with db_mod.SessionLocal() as session:
            t = await session.get(StagingTransfer, transfer_id)
            cmd = (
                await session.get(AgentCommand, t.command_id) if t is not None else None
            )

        if t is None:
            yield ServerSentEvent(event="error", data={"detail": "transfer not found"})
            return

        waiting = _waiting_for_agent(t, cmd)
        data = {**_status_payload(t), "waiting_for_agent": waiting}

        if t.state in _TERMINAL_STATES:
            reason = _terminal_reason(t, cmd)
            data["reason"] = reason
            if reason == "offline_timeout":
                yield ServerSentEvent(event="offline_timeout", data=data)
            elif t.state == "failed":
                yield ServerSentEvent(
                    event="error",
                    data={**data, "detail": "Transfer failed integrity verification"},
                )
            # Always emit the terminal snapshot (even if unchanged) so a late
            # client still gets a clean close.
            yield ServerSentEvent(event="done", data=data)
            return

        # Change-detection on the meaningful fields only (NOT the derived
        # timestamps), so an idle transfer lets the framework keepalive take over.
        key = json.dumps(
            {
                "state": t.state,
                "bytes": t.bytes_transferred,
                "total": t.total_bytes,
                "verified": t.verified,
                "waiting": waiting,
            },
            sort_keys=True,
        )
        if key != last_key:
            last_key = key
            yield ServerSentEvent(event="progress", data=data)

        await asyncio.sleep(_SSE_POLL_INTERVAL)


@router.get("/transfers/{transfer_id}/download")
async def download_transfer(
    transfer_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("download", coarse="write")),
) -> FileResponse:
    """Stream the VERIFIED staged file (brief §2.2/§6): Range-capable, served with
    a ``Content-Disposition`` attachment (filename = the item's filename).

    Served ONLY when ``verified=True`` AND ``state in (staged, downloaded)``;
    otherwise 409 with a reason that distinguishes still-uploading from
    failed-verification. Enforces the RBAC ``download`` action on the item BEFORE
    serving. Stamps ``last_range_request_at`` on EVERY request (the P10-T8 sweep
    watermark) and flips ``staged → downloaded`` once a response covers the final
    byte. Re-download stays allowed until TTL. Audited unconditionally (R2)."""
    t = await _load_transfer(session, transfer_id)
    item = await session.get(Item, t.item_id)
    if item is None:  # pragma: no cover - item FK cascades with the transfer
        raise HTTPException(404, "Item not found")
    ctx.authorize_item(item, action="download")

    if not (t.verified and t.state in ("staged", "downloaded")):
        if t.state == "failed":
            raise HTTPException(409, "Transfer failed integrity verification")
        if t.state in ("pending", "uploading"):
            raise HTTPException(409, "Transfer is still uploading; not yet available")
        if t.state == "expired":
            raise HTTPException(409, "Transfer has expired")
        # staged but unverified (verification not requested/completed).
        raise HTTPException(409, "Transfer is not verified; download withheld")

    exists, size = _staged_stat(t.staged_path)
    if not exists:
        raise HTTPException(409, "Staged file is no longer available")

    range_header = request.headers.get("range")
    now = datetime.now(UTC)
    t.last_range_request_at = now
    if t.state == "staged" and _range_covers_final_byte(range_header, size):
        t.state = transfers.transfer_state_machine(t.state, "download")
    await session.commit()

    await audit.emit(
        audit.AGENT_TRANSFER_DOWNLOADED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "transfer_id": str(t.id),
            "item_id": str(t.item_id),
            "agent_id": str(t.agent_id),
            "range": range_header,
            "state": t.state,
        },
    )
    return FileResponse(
        t.staged_path,
        media_type="application/octet-stream",
        filename=item.filename,
    )


@router.delete("/transfers/{transfer_id}", dependencies=[Depends(require_scope("write"))])
async def cancel_transfer(
    transfer_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cancel an in-flight transfer (brief §2.5): tombstone the underlying command
    (P10-T1 cancel transition), move the transfer to its terminal ``expired`` state
    (the transfer machine's operator-teardown terminal — there is no distinct
    ``cancelled`` transfer state), delete any staged bytes, audit. Cancelling an
    already-terminal transfer → 409. ``write`` scope."""
    t = await _load_transfer(session, transfer_id)
    if t.state in ("downloaded", "expired", "failed"):
        raise HTTPException(409, f"Transfer already {t.state}")

    now = datetime.now(UTC)
    cmd = await session.get(AgentCommand, t.command_id)
    if cmd is not None and not agentsync.command_is_terminal(cmd.status):
        cmd.status = agentsync.command_state_machine(cmd.status, "cancel")
        cmd.completed_at = now
        cmd.updated_at = now

    t.state = transfers.transfer_state_machine(t.state, "expire")
    if t.staged_path:
        try:
            os.remove(t.staged_path)
        except OSError:  # pragma: no cover - best-effort reclaim
            pass
    await session.commit()

    await audit.emit(
        audit.AGENT_TRANSFER_CANCELLED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "transfer_id": str(t.id),
            "command_id": str(t.command_id),
            "item_id": str(t.item_id),
        },
    )
    return {"transfer_id": str(t.id), "state": t.state}
