"""P12-T13 — agent-side thumbnail upload (central half).

An agent holds the source files central cannot reach (they live on the agent's
host), so it generates thumbnails itself and PUSHES the encoded bytes here. This
is a dedicated **small-blob** endpoint, NOT the Phase-10 ``stage_upload`` staging
channel — deliberately:

  * A thumbnail is ~10-60 KiB; the resumable tus-subset staging plane (attach →
    offset-PATCH → verify) is sized for multi-GB media and would be pure overhead.
  * P10-T5 staging verification RE-HASHES the staged bytes and compares to the
    item's catalog ``content_hash`` — a thumbnail hashes DIFFERENTLY from its
    source, so it could never pass that check. The staging plane is structurally
    wrong for derivatives.
  * There is no command to attach to: the agent generates thumbnails proactively
    (a post-scan pass), not in response to a central command.

Contract (brief §6): no ordering requirement, retryable, idempotent by the
content-addressed key (a retried upload of the same key is a write-if-absent
no-op). Validation is fail-closed: size cap, image-magic sniff, and item
resolution UNDER THE UPLOADING AGENT's library (which authorises ownership by
construction — the agent's local ids never cross the wire, so the item is
addressed by ``(library_ref, rel_path)`` exactly as the command/replication planes
address it). Central re-derives the cache key from ITS OWN catalog row and refuses
a mismatch with the agent-declared key (an honest guard against a hash race).

The bytes are stored VERBATIM under central's ``<key>.webp`` storage name (so
``abs_path`` / orphan-GC address them unchanged). The agent encodes JPEG (no
pure-Go lossy WebP under CGO_ENABLED=0); central's serve path sniffs the magic
bytes and sets the correct Content-Type — no manifest/DB change is needed because
thumbnail serving is purely content-addressed by the manifest's ``cache_key``.
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import thumbs as th
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.diskguard import DiskGuardError
from filearr.models import Agent, Item, Library

router = APIRouter()

log = logging.getLogger("filearr.thumbs")

_HDR_LIBRARY_REF = "X-Filearr-Library-Ref"
_HDR_REL_PATH = "X-Filearr-Rel-Path"
_HDR_TIER = "X-Filearr-Thumb-Tier"
_HDR_KEY = "X-Filearr-Thumb-Key"
_HDR_WIDTH = "X-Filearr-Thumb-Width"
_HDR_HEIGHT = "X-Filearr-Thumb-Height"

# smallint bound for the manifest width/height columns.
_SMALLINT_MAX = 32767


def _sniff_image(data: bytes) -> str | None:
    """Return an image kind ('webp'|'jpeg'|'png'|'gif') from the magic bytes, or
    ``None`` when the blob is not a recognised image container. A hostile agent
    cannot store arbitrary bytes as a thumbnail (brief §6: image-decodability)."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def _read_capped(request: Request, cap: int) -> bytes:
    """Read the request body, refusing (413) once it exceeds ``cap`` bytes — the
    body is never materialised unboundedly."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"thumbnail exceeds {cap} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _resolve_item(
    session: AsyncSession, agent: Agent, library_ref: str, rel_path: str
) -> Item:
    """Resolve the item addressed by (library_ref, rel_path) UNDER the uploading
    agent's library. The library is looked up by ``(source_agent_id=agent.id,
    agent_library_ref=library_ref)`` — the SAME keying ``apply_batch`` uses — so a
    resolved item is owned by this agent by construction (no wrong-agent write, no
    existence leak: an unknown/foreign item is an indistinguishable 404). A 404
    also covers the normal "not replicated to central yet" race — the agent
    retries on a later pass."""
    library = (
        await session.execute(
            select(Library).where(
                Library.source_agent_id == agent.id,
                Library.agent_library_ref == library_ref,
            )
        )
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such library for this agent")
    item = (
        await session.execute(
            select(Item).where(Item.library_id == library.id, Item.rel_path == rel_path)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")
    return item


def _parse_dim(raw: str | None) -> int | None:
    """Parse an optional width/height header into a smallint-safe int, or None."""
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    if v < 0 or v > _SMALLINT_MAX:
        return None
    return v


@router.post(
    "/agents/{agent_id}/thumbs",
    dependencies=[Depends(require_agents_enabled)],
)
async def upload_thumb(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Store an agent-generated thumbnail blob (write-if-absent, idempotent).

    201 on a fresh store, 200 on a no-op (the content-addressed key already
    exists). Body = raw encoded image bytes; the item ref + tier + declared key ride
    in headers."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    if not settings.thumbs_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thumbnails are disabled")

    tier = th.tier_from_name(request.headers.get(_HDR_TIER, "grid"))
    if tier is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid tier (expected 'grid' or 'preview')"
        )
    library_ref = urllib.parse.unquote_plus(request.headers.get(_HDR_LIBRARY_REF, ""))
    rel_path = urllib.parse.unquote_plus(request.headers.get(_HDR_REL_PATH, ""))
    if not library_ref or not rel_path:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{_HDR_LIBRARY_REF} and {_HDR_REL_PATH} headers required",
        )

    data = await _read_capped(request, settings.thumbnail_agent_max_bytes)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty thumbnail body")
    if _sniff_image(data) is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "body is not a recognised image"
        )

    item = await _resolve_item(session, agent, library_ref, rel_path)

    # Central re-derives the key from ITS catalog row; an item with no hash yet has
    # no addressable slot (409 — the agent retries once extraction sets a hash).
    hash_used = item.content_hash or item.quick_hash
    if not hash_used:
        raise HTTPException(status.HTTP_409_CONFLICT, "item not yet hashed")
    expected_key = th.cache_key(hash_used, settings.thumbnail_generator_version, tier)
    declared = request.headers.get(_HDR_KEY)
    if declared and declared != expected_key:
        # The agent's local hash briefly disagrees with central's catalog row (a
        # replication race). Refuse rather than store a mis-keyed thumbnail; the
        # agent retries once its replicated hash lands.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "declared key does not match the catalog"
        )

    from filearr.tasks.thumbs import store_agent_thumb

    try:
        result = await store_agent_thumb(
            session,
            item,
            tier,
            data,
            width=_parse_dim(request.headers.get(_HDR_WIDTH)),
            height=_parse_dim(request.headers.get(_HDR_HEIGHT)),
            settings=settings,
        )
    except DiskGuardError as err:
        # The cache filesystem is at the critical low-space floor (FIX-11). A
        # thumbnail is disposable — refuse the write with 507 so the agent retries
        # later, never a 500.
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE, "thumbnail cache is low on disk"
        ) from err
    if result is None:  # pragma: no cover - guarded by the hash check above
        raise HTTPException(status.HTTP_409_CONFLICT, "item not yet hashed")
    row, created = result
    await session.commit()
    return JSONResponse(
        {"stored": True, "cache_key": row.cache_key, "tier": tier},
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )
