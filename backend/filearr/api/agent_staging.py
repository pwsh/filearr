"""P10-T4 — the resumable agent->central staging data plane (central half).

The agent-plane receiver for a ``stage_upload``: after an agent picks up a
``stage_upload`` command (P10-T1 queue), it *attaches* a ``staging_transfers``
row (idempotent per ``command_id``) and streams the file body in chunks. The wire
protocol is a **hand-rolled tus SUBSET** (offset-``PATCH``), chosen over full tus
in the R6 sizing spike (docs/research/phase-10-t4-transport-spike.md): tus's
resume discipline (a monotone committed offset, ``Upload-Offset`` echoed on every
request, 409-on-wrong-offset) with none of tus's Creation/Concatenation/Checksum/
metadata surface or a third-party server dependency.

Wire contract (all behind ``FILEARR_AGENTS_ENABLED`` -> 404 off, agent-plane
auth via :func:`api.agent_commands._authenticate_agent`):

* ``POST /agents/{agent_id}/staging`` — attach: body ``{command_id, total_bytes}``.
  Creates-or-returns the single transfer row for that ``stage_upload`` command
  (idempotent). ``item_id`` / ``agent_id`` are taken from the COMMAND, never the
  body (an agent cannot stage a transfer for an item it does not host). 201 on
  create, 200 on re-attach. Body: the :class:`StagingStatus` (incl. the committed
  ``bytes_transferred`` = the resume point).
* ``HEAD /agents/{agent_id}/staging/{transfer_id}`` — offset query: ``Upload-Offset``
  = committed bytes, ``Upload-Total`` = declared size, ``Upload-State`` = state.
  The tus HEAD equivalent; the resume point of record.
* ``PATCH /agents/{agent_id}/staging/{transfer_id}`` — append: header
  ``Upload-Offset`` MUST equal the committed ``bytes_transferred``, else **409**
  with the current offset (``Upload-Offset`` header + JSON ``{reason, offset}``).
  The raw body is appended to the staged file; ``bytes_transferred`` advances
  ONLY after the write is fsynced (so the committed offset is always a durable
  prefix — a dropped connection loses only the un-acked tail). A chunk past
  ``total_bytes`` is refused. On the final byte the row goes ``staged`` (through
  ``transfers.transfer_state_machine``) with ``verified=False`` — integrity
  verification is the P10-T5 seam, deliberately NOT implemented here.

The row is locked ``FOR UPDATE`` for the duration of a PATCH so two racing
appends serialise (the loser sees the advanced offset and 409s) — the belt to the
agent-side 1-upload/agent concurrency cap's braces.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, transfers
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import Settings, get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, Item, Library, StagingTransfer
from filearr.tasks import extract

router = APIRouter()

_HDR_OFFSET = "Upload-Offset"
_HDR_TOTAL = "Upload-Total"
_HDR_STATE = "Upload-State"

# Non-terminal states from which an append may still flow.
_APPENDABLE = ("pending", "uploading")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def staging_root(settings: Settings) -> Path:
    """The configured staging directory (R5: writable central disk, default
    ``{config_dir}/staging`` — NOT a media mount)."""
    return Path(settings.staging_dir or f"{settings.config_dir}/staging")


def staged_path(settings: Settings, transfer_id: uuid.UUID | str) -> Path:
    """Absolute on-disk path for a transfer's staged body. The FILENAME is
    ``transfers.staging_path_for``'s (``<uuid>.bin``) — traversal-proof by
    construction (the id is UUID-validated, so no ``..`` / separator can escape);
    only the DIRECTORY is swapped for the configured, real-filesystem root."""
    name = PurePosixPath(transfers.staging_path_for(transfer_id)).name
    return staging_root(settings) / name


def _status_dict(t: StagingTransfer) -> dict:
    return {
        "id": str(t.id),
        "item_id": str(t.item_id),
        "agent_id": str(t.agent_id),
        "command_id": str(t.command_id),
        "state": t.state,
        "bytes_transferred": t.bytes_transferred,
        "total_bytes": t.total_bytes,
        "verified": t.verified,
    }


def _status_headers(t: StagingTransfer) -> dict[str, str]:
    h = {_HDR_OFFSET: str(t.bytes_transferred), _HDR_STATE: t.state}
    if t.total_bytes is not None:
        h[_HDR_TOTAL] = str(t.total_bytes)
    return h


async def _load_transfer(
    session: AsyncSession, agent: Agent, transfer_id: uuid.UUID
) -> StagingTransfer:
    """Load a transfer that MUST belong to ``agent`` — a wrong-agent / unknown id
    is a 404 (never leak another agent's transfer)."""
    t = await session.get(StagingTransfer, transfer_id)
    if t is None or t.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such transfer")
    return t


# --------------------------------------------------------------------------- #
# P10-T5 — integrity verification on staging completion                        #
# --------------------------------------------------------------------------- #
# When the final byte commits (the `staged` transition), the staged file is
# RE-READ from disk in a SINGLE streaming pass that computes BOTH the full xxh3
# content hash and the quick hash, and the result is checked against the catalog
# row before any download is ever served.
#
# DELIBERATE DEVIATION from the P10-T5 task text ("streaming hash folded into the
# upload write path"): P10-T4's resume discipline truncates the staged file back
# to the committed offset on every PATCH, so an incremental hash carried across
# chunks would be unsound (a resumed/replayed tail would corrupt the running
# digest). A fresh disk re-read on the final transition is the ONLY always-correct
# source, and it is the same bytes we are about to serve. We call
# ``tasks.extract.full_hash`` / ``quick_hash`` DIRECTLY (rather than re-deriving
# the digests here) so the computed values are byte-identical to what the scanner
# stored — the QH-T2/T3 contract (``content`` = xxh3-128 whole-file;
# ``quick`` = xxh3-64, whole file <=128 KiB else head+tail 64 KiB) lives in one
# place and a future contract change can never make this comparison false-fail.
#
# Gate: verification runs only when the transfer's ``stage_upload`` command payload
# carries a truthy ``verify`` flag (set by the P10-T13 initiate path from
# ``TransferRequest.verify_hash``, default True). A stage_upload enqueued without
# it (e.g. the P10-T4 raw agent-plane tests / a bare operator enqueue) stages with
# ``verified=False`` exactly as before — the pre-T5 behaviour, unchanged.


def _compute_staged_hashes(path: Path) -> tuple[str, str, str, int]:
    """``(content_hash_128, content_hash_legacy64, quick_hash, size_on_disk)`` for
    the staged file, computed by the exact scanner functions
    (``tasks.extract.full_hashes_migration`` / ``quick_hash``) so the values are
    byte-identical to the catalog's. Both full digests come from ONE whole-file
    pass (the QH-T3 migration-window helper): catalog rows hashed pre-xxh3-128
    hold 16-hex xxh3-64 values until the cfg2 sweep (or an agent-side rescan)
    replaces them, and the comparison must dispatch on the stored length rather
    than false-fail every not-yet-rehashed item. ``quick_hash`` reads at most
    128 KiB for a large file — effectively a single pass over the just-written,
    page-cache-hot bytes."""
    spath = str(path)
    size = path.stat().st_size
    h128, h64 = extract.full_hashes_migration(spath, size)
    return h128, h64, extract.quick_hash(spath, size), size


def _verify_against_catalog(
    item: Item, content_hash: str, content_hash_legacy: str, quick_hash: str, size_on_disk: int
) -> tuple[bool, str | None, str | None, str | None]:
    """Compare the computed staged hashes/size against the catalog row.

    Returns ``(ok, reason, expected_hash, computed_hash)``. Fail-closed
    (integrity > availability): size must equal ``item.size`` AND the catalog's
    strongest hash (``content_hash`` when non-NULL, else ``quick_hash``) must
    match its computed counterpart. An item with NEITHER hash fails with
    ``no_catalog_hash`` — an unverifiable file is never served.

    Digest-length dispatch (QH-T3 migration window): a 32-hex stored
    ``content_hash`` compares against the computed xxh3-128; anything shorter is
    a legacy xxh3-64 value and compares against the computed legacy digest — a
    legitimate upload of a not-yet-rehashed item must not false-fail. Both were
    computed in the same read, so neither branch weakens the byte check."""
    if size_on_disk != item.size:
        return False, "size_mismatch", str(item.size), str(size_on_disk)
    if item.content_hash is not None:
        computed = content_hash if len(item.content_hash) == 32 else content_hash_legacy
        ok = computed == item.content_hash
        return ok, (None if ok else "content_hash_mismatch"), item.content_hash, computed
    if item.quick_hash is not None:
        ok = quick_hash == item.quick_hash
        return ok, (None if ok else "quick_hash_mismatch"), item.quick_hash, quick_hash
    return False, "no_catalog_hash", None, content_hash


async def _pending_rehash_exists(session: AsyncSession, item_id: uuid.UUID) -> bool:
    """True when a ``rehash_check`` command for ``item_id`` is already
    pending/picked_up — guards against piling duplicate self-correction commands."""
    row = (
        await session.execute(
            select(AgentCommand.id)
            .where(
                AgentCommand.item_id == item_id,
                AgentCommand.kind == "rehash_check",
                AgentCommand.status.in_(("pending", "picked_up")),
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _enqueue_rehash_check(
    session: AsyncSession, item: Item, agent_id: uuid.UUID, settings: Settings, now: datetime
) -> bool:
    """Enqueue a ``rehash_check`` (P10-T1 machinery) so the catalog self-corrects
    the drifted hash (P10-T3 reconcile handles the completion). Skips when one is
    already in flight for the item. Payload keys match the Go executor
    (``library_ref`` = the library's agent-side root, ``rel_path``, ``content``)."""
    if await _pending_rehash_exists(session, item.id):
        return False
    library = await session.get(Library, item.library_id)
    library_ref = library.root_path if library is not None else ""
    ttl = settings.agent_command_ttl_seconds
    session.add(
        AgentCommand(
            agent_id=agent_id,
            kind="rehash_check",
            item_id=item.id,
            payload={"library_ref": library_ref, "rel_path": item.rel_path, "content": True},
            status="pending",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
    )
    return True


async def _finalize_staged(
    session: AsyncSession,
    t: StagingTransfer,
    settings: Settings,
    request: Request,
) -> None:
    """Drive the terminal transition on the final byte (state is ``uploading``).

    Without a ``verify`` request: ``uploading → staged`` (``verified=False``), the
    pre-P10-T5 behaviour. With verification: re-read the staged file, compare to
    the catalog, and either ``→ staged`` (``verified=True``, in THIS transaction)
    on a match, or ``→ failed`` on a mismatch/unverifiable file — deleting the
    staged bytes (never serve them), enqueuing a self-correcting ``rehash_check``,
    and auditing expected-vs-computed. Does NOT commit (the caller commits)."""
    cmd = await session.get(AgentCommand, t.command_id)
    do_verify = bool((cmd.payload or {}).get("verify")) if cmd is not None else False
    if not do_verify:
        t.state = transfers.transfer_state_machine(t.state, "staged")
        return

    item = await session.get(Item, t.item_id)
    path = Path(t.staged_path) if t.staged_path else staged_path(settings, t.id)
    content_hash, content_hash_legacy, quick_hash, size_on_disk = _compute_staged_hashes(path)
    ok = False
    reason = expected = computed = None
    if item is not None:
        ok, reason, expected, computed = _verify_against_catalog(
            item, content_hash, content_hash_legacy, quick_hash, size_on_disk
        )
    else:  # pragma: no cover - the item FK is ON DELETE CASCADE with the transfer
        reason, computed = "no_item", content_hash

    if ok:
        t.state = transfers.transfer_state_machine(t.state, "staged")
        t.verified = True
        t.verified_hash = content_hash
        return

    # Mismatch / unverifiable: fail-closed. Fail the transfer, drop the bytes,
    # enqueue self-correction, audit. uploading --fail--> failed (existing edge).
    now = datetime.now(UTC)
    t.state = transfers.transfer_state_machine(t.state, "fail")
    t.verified = False
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort disk hygiene
        pass
    enqueued = False
    if item is not None:
        enqueued = await _enqueue_rehash_check(session, item, t.agent_id, settings, now)
    await audit.emit(
        audit.AGENT_TRANSFER_VERIFY_FAILED,
        request=request,
        details={
            "transfer_id": str(t.id),
            "item_id": str(t.item_id),
            "agent_id": str(t.agent_id),
            "reason": reason,
            "expected": expected,
            "computed": computed,
            "size_on_disk": size_on_disk,
            "rehash_enqueued": enqueued,
        },
    )


# --------------------------------------------------------------------------- #
# Attach — create-or-return the transfer for a stage_upload command            #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/staging",
    dependencies=[Depends(require_agents_enabled)],
)
async def attach_transfer(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Attach (create-or-return) the staging transfer for a picked-up
    ``stage_upload`` command. Idempotent per ``command_id`` (a restarted agent, or
    the at-least-once command redelivery, re-attaches the SAME row and resumes
    from its committed offset). 201 on create, 200 on re-attach."""
    agent = await _authenticate_agent(session, agent_id, request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "body must be an object")
    try:
        command_id = uuid.UUID(str(body["command_id"]))
    except (KeyError, ValueError, TypeError) as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "command_id must be a UUID"
        ) from err
    total_bytes = body.get("total_bytes")
    if total_bytes is not None and (not isinstance(total_bytes, int) or total_bytes < 0):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "total_bytes must be a non-negative int"
        )

    settings = get_settings()
    # Re-attach: an existing row for this command wins regardless of the command's
    # current status (the upload may have out-lived the lease and been redelivered).
    existing = (
        await session.execute(
            select(StagingTransfer).where(StagingTransfer.command_id == command_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # A size disagreement mid-resume means the source changed — refuse rather
        # than splice a new file's bytes onto a stale prefix (integrity > speed).
        if (
            total_bytes is not None
            and existing.total_bytes is not None
            and total_bytes != existing.total_bytes
            and existing.bytes_transferred > 0
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"total_bytes changed since attach (was {existing.total_bytes}, "
                f"now {total_bytes}); restart the transfer",
            )
        if existing.bytes_transferred == 0 and total_bytes is not None:
            existing.total_bytes = total_bytes
            await session.commit()
        return JSONResponse(
            _status_dict(existing),
            status_code=status.HTTP_200_OK,
            headers=_status_headers(existing),
        )

    cmd = await session.get(AgentCommand, command_id)
    if cmd is None or cmd.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    if cmd.kind != "stage_upload":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command {command_id} is not a stage_upload"
        )
    if cmd.status != "picked_up":
        # Only an in-flight (leased) command may open a transfer; a terminal or
        # never-delivered command must not.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command not in-flight (is {cmd.status})"
        )

    tid = uuid.uuid4()
    path = staged_path(settings, tid)
    now = datetime.now(UTC)
    t = StagingTransfer(
        id=tid,
        item_id=cmd.item_id,
        agent_id=agent.id,
        command_id=command_id,
        state="pending",
        bytes_transferred=0,
        total_bytes=total_bytes,
        staged_path=str(path),
        verified=False,
        expires_at=now + timedelta(seconds=settings.staging_transfer_ttl_seconds),
        created_at=now,
    )
    session.add(t)
    try:
        await session.commit()
    except IntegrityError:
        # Race backstop for the two unique constraints on this table:
        #   * uq_staging_transfers_command — two concurrent attaches for the SAME
        #     command; the loser re-reads and returns the winner's row (idempotent
        #     re-attach, 200), never a duplicate.
        #   * uq_staging_transfers_active_item (P10-T6) — a second ACTIVE transfer
        #     for the same item (a stray extra stage_upload command); refuse with
        #     409 rather than a 500.
        await session.rollback()
        winner = (
            await session.execute(
                select(StagingTransfer).where(
                    StagingTransfer.command_id == command_id
                )
            )
        ).scalar_one_or_none()
        if winner is not None:
            return JSONResponse(
                _status_dict(winner),
                status_code=status.HTTP_200_OK,
                headers=_status_headers(winner),
            )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an active transfer already exists for this item",
        ) from None
    return JSONResponse(
        _status_dict(t),
        status_code=status.HTTP_201_CREATED,
        headers=_status_headers(t),
    )


# --------------------------------------------------------------------------- #
# Offset query (tus HEAD equivalent)                                           #
# --------------------------------------------------------------------------- #
@router.head(
    "/agents/{agent_id}/staging/{transfer_id}",
    dependencies=[Depends(require_agents_enabled)],
)
async def head_transfer(
    agent_id: uuid.UUID,
    transfer_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The committed-offset query. ``Upload-Offset`` is the resume point of
    record (== ``bytes_transferred``); ``Upload-Total`` / ``Upload-State`` echo
    the declared size and lifecycle state. No body (HEAD)."""
    agent = await _authenticate_agent(session, agent_id, request)
    t = await _load_transfer(session, agent, transfer_id)
    return Response(status_code=status.HTTP_200_OK, headers=_status_headers(t))


# --------------------------------------------------------------------------- #
# Append (tus PATCH equivalent)                                                #
# --------------------------------------------------------------------------- #
@router.patch(
    "/agents/{agent_id}/staging/{transfer_id}",
    dependencies=[Depends(require_agents_enabled)],
)
async def append_transfer(
    agent_id: uuid.UUID,
    transfer_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Append a chunk at ``Upload-Offset``. The offset MUST equal the committed
    ``bytes_transferred`` (else 409 + the current offset — the at-least-once retry
    contract). Bytes stream to the staged file; ``bytes_transferred`` advances
    only after the write is fsynced. On the final byte the row goes ``staged``
    (``verified=False``; P10-T5 verifies). The row is locked ``FOR UPDATE`` so
    concurrent appends serialise."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()

    # Lock the row for the whole append so a racing PATCH serialises behind it.
    t = (
        await session.execute(
            select(StagingTransfer)
            .where(StagingTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if t is None or t.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such transfer")

    # Idempotent completion replay: a PATCH to an already-staged/downloaded row is
    # a 200 no-op returning the current status (the ack was lost, the agent
    # retried). Keyed on STATE, not bytes, so a legitimate final chunk of a
    # zero-byte file (bytes==total==0 but still `pending`) is NOT swallowed.
    if t.state in ("staged", "downloaded"):
        return JSONResponse(
            _status_dict(t), status_code=status.HTTP_200_OK, headers=_status_headers(t)
        )
    if t.state not in _APPENDABLE:  # expired / failed
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"transfer not appendable (is {t.state})"
        )

    offset_hdr = request.headers.get(_HDR_OFFSET)
    if offset_hdr is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{_HDR_OFFSET} header required"
        )
    try:
        offset = int(offset_hdr)
    except ValueError as err:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{_HDR_OFFSET} must be an integer"
        ) from err
    if offset != t.bytes_transferred:
        # tus 409 discipline: hand back the authoritative offset so the agent
        # re-seeks and retries (idempotent under the at-least-once command queue).
        return JSONResponse(
            {"reason": "offset_mismatch", "offset": t.bytes_transferred},
            status_code=status.HTTP_409_CONFLICT,
            headers={_HDR_OFFSET: str(t.bytes_transferred)},
        )

    path = Path(t.staged_path) if t.staged_path else staged_path(settings, t.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    max_chunk = settings.staging_max_chunk_bytes
    limit = t.total_bytes

    # Open, discard any un-acked tail from a crashed prior PATCH (truncate to the
    # committed offset), then append. bytes_transferred is advanced ONLY after the
    # fsync below, so what is on disk up to `offset` is always a durable prefix.
    # O_BINARY (Windows dev host) — never CRLF-translate a media byte stream.
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    written = 0
    try:
        os.ftruncate(fd, offset)
        os.lseek(fd, offset, os.SEEK_SET)
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > max_chunk:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"chunk exceeds {max_chunk} bytes",
                )
            if limit is not None and offset + written > limit:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"chunk would exceed total_bytes ({limit})",
                )
            os.write(fd, chunk)
        os.fsync(fd)
    except HTTPException:
        # A rejected chunk leaves the committed prefix intact on disk (we truncated
        # to `offset` and only appended past it); bytes_transferred is unchanged,
        # so the agent simply re-queries and retries from `offset`.
        os.close(fd)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    new_offset = offset + written
    # Advance the lifecycle through the frozen state machine (guarded: an invalid
    # transition surfaces as a 500, never a silent bad state). On the final byte,
    # ``_finalize_staged`` performs the P10-T5 integrity verification (re-read +
    # hash-check) and drives the terminal transition (``staged`` on a match,
    # ``failed`` on a mismatch — never serving unverified bytes).
    t.bytes_transferred = new_offset
    try:
        if t.state == "pending":
            t.state = transfers.transfer_state_machine(t.state, "start_upload")
        if limit is not None and new_offset >= limit:
            await _finalize_staged(session, t, settings, request)
    except ValueError as err:  # pragma: no cover - guarded invariant
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"invalid transfer transition: {err}"
        ) from err
    await session.commit()
    return JSONResponse(
        _status_dict(t), status_code=status.HTTP_200_OK, headers=_status_headers(t)
    )
