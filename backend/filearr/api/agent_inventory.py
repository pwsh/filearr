"""W6-D3 — agent inventory-results receiver (central half).

The dedicated small-blob channel for a LARGE inventory command result. A small
result inlines its ``{summary, entries}`` in the command completion JSONB
(``agent_command_result_max_bytes``); a result above the agent's inline threshold
is gzip-NDJSON-uploaded HERE and the completion carries only ``{summary,
result_ref}``.

This mirrors :mod:`filearr.api.agent_thumbs`' write-if-absent posture — NOT the
Phase-10 ``stage_upload`` staging plane — deliberately:

  * An inventory blob is a metadata report (KiB–low-MiB), not multi-GB media; the
    resumable tus-subset staging plane would be pure overhead.
  * Staging RE-HASHES the bytes against an item's catalog ``content_hash``; an
    inventory NDJSON blob has no such catalog counterpart, so that verification is
    structurally wrong for it.

Contract:

* ``POST /agents/{agent_id}/inventory-results`` — body: raw gzip NDJSON; headers:
  ``X-Filearr-Command-Id`` (the inventory command this result belongs to).
  Agent-plane auth (:func:`api.agent_commands._authenticate_agent`), behind
  ``FILEARR_AGENTS_ENABLED`` (404 off). The command MUST belong to the uploading
  agent (wrong-agent / unknown ⇒ 404 — never leak another agent's command) and be
  an ``inventory`` command (409 otherwise). Size-capped (413), gzip-magic sniffed
  (415). Idempotent write-if-absent: 201 on a fresh store, 200 when the
  content-addressed file already exists (a redelivered command re-uploads the same
  bytes). Stored VERBATIM at ``{inventory_dir}/<command_id>.ndjson.gz``; the
  returned ``result_ref`` is the stable relative name the completion records.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import Settings, get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand

router = APIRouter()

_HDR_COMMAND_ID = "X-Filearr-Command-Id"

# gzip magic bytes — a fail-closed check the blob is what the contract declares.
_GZIP_MAGIC = b"\x1f\x8b"


def inventory_dir(settings: Settings) -> Path:
    """The configured inventory-results directory (default ``{config_dir}/inventory``
    — writable central disk, never a media mount; invariant 6)."""
    return Path(settings.inventory_dir or f"{settings.config_dir}/inventory")


def result_path(settings: Settings, command_id: uuid.UUID) -> Path:
    """Absolute on-disk path for a command's stored result. The filename derives
    ONLY from the UUID-validated command id, so no traversal can escape the dir."""
    return inventory_dir(settings) / f"{command_id}.ndjson.gz"


async def _read_capped(request: Request, cap: int) -> bytes:
    """Read the request body, refusing (413) once it exceeds ``cap`` bytes — never
    materialised unboundedly."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"inventory result exceeds {cap} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _owned_inventory_command(
    session: AsyncSession, agent: Agent, command_id: uuid.UUID
) -> AgentCommand:
    """Load an ``inventory`` command that MUST belong to ``agent`` — a wrong-agent
    or unknown id is a 404 (never leak another agent's command); a non-inventory
    command is a 409."""
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None or cmd.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    if cmd.kind != "inventory":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command {command_id} is not an inventory command"
        )
    return cmd


@router.post(
    "/agents/{agent_id}/inventory-results",
    dependencies=[Depends(require_agents_enabled)],
)
async def upload_inventory_result(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Store a large inventory result blob (gzip NDJSON), write-if-absent.

    201 on a fresh store, 200 when the file already exists (idempotent redelivery).
    Returns ``{result_ref}`` — the stable relative name the command completion
    records alongside its summary."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()

    raw_cid = request.headers.get(_HDR_COMMAND_ID)
    if not raw_cid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{_HDR_COMMAND_ID} header required"
        )
    try:
        command_id = uuid.UUID(raw_cid)
    except ValueError as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{_HDR_COMMAND_ID} must be a UUID"
        ) from err

    cmd = await _owned_inventory_command(session, agent, command_id)

    data = await _read_capped(request, settings.agent_inventory_result_max_bytes)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty result body")
    if data[:2] != _GZIP_MAGIC:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "body is not a gzip stream"
        )

    path = result_path(settings, cmd.id)
    ref = f"inventory/{cmd.id}.ndjson.gz"
    # Write-if-absent: a redelivered command re-uploads the SAME bytes; the first
    # store wins and later ones are 200 no-ops. Written to a temp sibling then
    # atomically renamed so a crash mid-write never leaves a truncated blob.
    if path.exists():
        return JSONResponse(
            {"stored": True, "result_ref": ref, "created": False},
            status_code=status.HTTP_200_OK,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a unique temp sibling then atomically rename so a crash mid-write
    # never leaves a truncated blob. Two concurrent first-uploads write identical
    # bytes and the last replace wins harmlessly (content-addressed by command id).
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as err:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE, "cannot store inventory result"
        ) from err
    return JSONResponse(
        {"stored": True, "result_ref": ref, "created": True},
        status_code=status.HTTP_201_CREATED,
    )
