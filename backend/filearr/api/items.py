"""Item read + metadata write API (partial-JSON PATCH, RFC 9457-style errors)."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr import thumbs as th
from filearr.config import get_settings
from filearr.custom_fields import (
    CustomFieldDef,
    applicable_defs,
    def_from_model,
    validate_custom_values,
)
from filearr.db import get_session
from filearr.embed import has_current_embedding, strip_embedding
from filearr.exif import strip_gps
from filearr.meili_ops import DEFAULT_EMBEDDER_NAME
from filearr.models import (
    AgentCommand,
    AgentShareMap,
    CustomField,
    Item,
    ItemVersion,
    Library,
    MediaType,
    ThumbnailManifest,
)
from filearr.schemas import (
    CopiesResponse,
    CopyCountsRequest,
    ItemCopy,
    ItemOut,
    ItemPatch,
)
from filearr.security import (
    PermissionContext,
    _verify_credentials,
    require_permission,
)
from filearr.worker import defer_index_sync

router = APIRouter()


async def _get_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    item = (await session.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


async def _load_custom_field_defs(session: AsyncSession) -> list[CustomFieldDef]:
    """Load every registered custom-field definition as pure ``CustomFieldDef``.

    The definition set is small (admin-defined), so a single unfiltered load per
    request is cheap; per-item applicability is narrowed in memory."""
    rows = (await session.execute(select(CustomField))).scalars().all()
    return [def_from_model(r) for r in rows]


def _validate_user_metadata_write(
    defs: list[CustomFieldDef], item: Item, incoming: dict
) -> None:
    """P4-T4: type-check the custom-field-governed values being WRITTEN.

    Only the values actually being set (non-null) are checked — an explicit null
    clears a key and unregistered keys pass through unvalidated (no behaviour
    change for ad-hoc metadata). Applicability is enforced: a definition scoped
    to other media types / libraries is excluded via ``applicable_defs``, so for
    this item its key counts as *unregistered* and is not validated. Per R3 a
    missing ``required`` field is NOT rejected. Raises a structured 422
    (``{detail: [{field, reason, field_source}]}``) on any violation."""
    to_check = {k: v for k, v in incoming.items() if v is not None}
    if not to_check:
        return
    here = applicable_defs(
        defs, media_type=item.media_type.value, library_id=str(item.library_id)
    )
    if not here:
        return
    errors = validate_custom_values(here, to_check)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[
                {"field": e.field, "reason": e.msg, "field_source": "custom_field"}
                for e in errors
            ],
        )


def _with_native_path(
    item: Item,
    library: Library | None,
    share_url: str | None = None,
    share_source: str | None = None,
) -> dict:
    out = ItemOut.model_validate(item, from_attributes=True)
    # P3-T11 GPS default-hidden gate (R5, CWE-1230): the server strips GPS/location
    # keys from the returned ``metadata`` unless the owning library opted in via
    # ``expose_gps``. This is the API-response half of the gate (build_doc is the
    # projection half); the Raw tab therefore never receives an unexposed GPS
    # coordinate (client never sees what the server didn't send). strip_gps is
    # non-mutating — the item's stored metadata_ (extracted truth) is untouched.
    if library is None or not library.expose_gps:
        out.metadata_ = strip_gps(out.metadata_)
    # P3-T8: never ship the internal semantic vector (~1.5 KB) or its fingerprint
    # to a client; they are index-side machinery, not user-facing metadata.
    out.metadata_ = strip_embedding(out.metadata_)
    if library is not None:
        # UI-T12: surface library name + user-facing share prefix so the detail
        # breadcrumb / open-location links can be built client-side.
        out.library_name = library.name
        # OPS-T7: effective share prefix (manual override wins, else the
        # deploy mount map covering the library root). The UI appends
        # rel_path to build open-location links exactly as before.
        from filearr import share_map

        eff, _src = share_map.effective_library_share(
            library.share_prefix, library.root_path
        )
        out.library_share_prefix = eff
        # UI-T15: Windows-UNC counterpart so the client can render either OS form.
        loc, _lsrc = share_map.effective_library_share_location(
            library.share_prefix, library.root_path
        )
        out.library_share_unc = loc.unc
        if library.native_prefix:
            sep = "\\" if "\\" in library.native_prefix else "/"
            out.native_path = (
                library.native_prefix.rstrip(sep) + sep + item.rel_path.replace("/", sep)
            )
    # P10-T11/T12: the effective network-open location (agent hint > agent mapping
    # > library share_prefix), resolved by the caller (needs a DB read for the
    # agent mappings) and threaded in.
    out.share_url = share_url
    out.share_source = share_source
    return out.model_dump(by_alias=True)


async def _resolve_item_share(
    session: AsyncSession, item: Item, library: Library | None
) -> tuple[str | None, str | None]:
    """Resolve the item's network-open location + source via the frozen precedence
    (:func:`filearr.share_resolution.resolve_item_share`). Loads the applicable
    ``agent_share_maps`` rows (agent-scoped + global) only for an agent-hosted item;
    a centrally-scanned item resolves against the library ``share_prefix`` / deploy
    mount map alone. Best-effort: never raises for a missing library."""
    from filearr.share_resolution import resolve_item_share
    from filearr.transfers import ShareMapping

    if library is None:
        return None, None

    mappings: list[ShareMapping] = []
    if item.source_agent_id is not None:
        rows = (
            await session.execute(
                select(AgentShareMap).where(
                    (AgentShareMap.agent_id == item.source_agent_id)
                    | (AgentShareMap.agent_id.is_(None))
                )
            )
        ).scalars().all()
        mappings = [
            ShareMapping(
                local_prefix=m.local_prefix,
                share_prefix=m.share_prefix,
                agent_id=str(m.agent_id) if m.agent_id else None,
                library_id=str(m.library_id) if m.library_id else None,
                unc=m.unc,
            )
            for m in rows
        ]

    return resolve_item_share(
        share_hint=item.share_hint,
        source_agent_id=str(item.source_agent_id) if item.source_agent_id else None,
        agent_mappings=mappings,
        library_share_prefix=library.share_prefix,
        library_root_path=library.root_path,
        item_path=item.path,
        rel_path=item.rel_path,
    )


@router.get("/{item_id}", response_model=ItemOut)
async def get_item(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
):
    item = await _get_item(session, item_id)
    ctx.authorize_item(item)  # 404 for an item outside the caller's read scope
    library = (
        await session.execute(select(Library).where(Library.id == item.library_id))
    ).scalar_one_or_none()
    share_url, share_source = await _resolve_item_share(session, item, library)
    return _with_native_path(item, library, share_url, share_source)


# --------------------------------------------------------------------------- #
# P10-T3 — agent stat/rehash verification request.                            #
#                                                                             #
# ``POST /items/{id}/verify`` enqueues an agent_commands row (P10-T1) asking   #
# the owning agent to confirm an agent-hosted item still exists / is unchanged.#
# The agent completes it (poll → stat/rehash → complete) and central          #
# reconciles the result inline in the complete endpoint (``filearr.verify``).  #
#                                                                             #
# RBAC (R2 — gate BEFORE the row is created): a ``stat`` verify needs the      #
# metadata-read action ``search_metadata``; a ``rehash`` (which spends agent   #
# CPU/IO hashing bytes) needs ``download``. Both are real members of the       #
# shipped RBAC action vocabulary (``filearr.rbac.ACTIONS``) — the task doc's   #
# wording ("search_metadata suffices" / "download required") maps 1:1 onto     #
# them, so no divergence. Both live under the coarse ``read`` scope, so the     #
# dependency gates coarse-read once and the per-mode fine action is enforced   #
# on the item below via ``authorize_item`` (404 not-visible / 403 visible-but- #
# denied, the standard split).                                                 #
# --------------------------------------------------------------------------- #
class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["stat", "rehash"]


_VERIFY_KIND = {"stat": "stat_check", "rehash": "rehash_check"}
_VERIFY_ACTION = {"stat": "search_metadata", "rehash": "download"}


@router.post("/{item_id}/verify")
async def verify_item(
    item_id: uuid.UUID,
    body: VerifyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    # Coarse-read gate + the resolved RBAC context (the per-mode fine action is
    # enforced on the item below). ``search_metadata`` is the least-privilege
    # coarse gate both modes share.
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict:
    """Request an agent re-verify this item's existence / integrity (P10-T3).

    404 unless agents are enabled AND the item's library is agent-owned. RBAC is
    enforced per mode BEFORE any command row is created (R2). Idempotent: 409 if
    an identical pending/picked-up verify command already exists for the item (no
    stacking). Returns the created ``agent_commands`` row; the result lands later
    via the normal item refresh once the agent completes it."""
    settings = get_settings()
    if not settings.agents_enabled:
        raise HTTPException(404, "Agent verification is not enabled")
    item = await _get_item(session, item_id)  # raw 404 when the item is gone
    action = _VERIFY_ACTION[body.mode]
    # RBAC per mode: 404 if the item is outside the caller's read scope; 403 if
    # readable but the mode's action (download for rehash) is denied.
    ctx.authorize_item(item, action=action)
    # Must be an agent-owned library (source_agent_id set) to have an agent to ask.
    library = (
        await session.execute(select(Library).where(Library.id == item.library_id))
    ).scalar_one_or_none()
    if library is None or library.source_agent_id is None:
        raise HTTPException(404, "Item is not hosted by an agent")
    agent_id = library.source_agent_id
    kind = _VERIFY_KIND[body.mode]

    # No stacking: an identical verify command already in flight is a 409.
    existing = (
        await session.execute(
            select(AgentCommand.id).where(
                AgentCommand.item_id == item.id,
                AgentCommand.kind == kind,
                AgentCommand.status.in_(("pending", "picked_up")),
            )
        )
    ).first()
    if existing is not None:
        raise HTTPException(409, f"a {kind} for this item is already pending")

    # Wire payload (P10-T3 contract): the agent resolves library_ref → its local
    # root, then rel_path under it. ``content`` only for a rehash (full hash).
    payload: dict = {
        "library_ref": library.agent_library_ref or library.root_path,
        "rel_path": item.rel_path,
    }
    if body.mode == "rehash":
        payload["content"] = True

    ttl = settings.agent_command_ttl_seconds
    now = datetime.now(UTC)
    aid = audit.actor_id(request)
    cmd = AgentCommand(
        agent_id=agent_id,
        kind=kind,
        item_id=item.id,
        payload=payload,
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=ttl),
        requested_by=uuid.UUID(aid) if aid else None,
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=aid,
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": kind,
            "item_id": str(item.id),
            "mode": body.mode,
            "verify": True,
        },
    )
    return {
        "id": str(cmd.id),
        "agent_id": str(agent_id),
        "kind": kind,
        "item_id": str(item.id),
        "status": cmd.status,
        "mode": body.mode,
        "expires_at": cmd.expires_at.isoformat(),
    }


# --- S12/P12 slice 1: thumbnail serving ------------------------------------- #
# Serve-path inline generation-on-miss (preview tier is lazy; a grid miss races a
# still-queued ride-along job) is bounded by a semaphore so a burst of misses on
# large/hostile images can't decode unboundedly inside request handlers. Created
# lazily on first use so it binds to the running loop with the configured size.
_thumb_bearer = HTTPBearer(auto_error=False)
_INLINE_SEM: asyncio.Semaphore | None = None


def _inline_sem() -> asyncio.Semaphore:
    global _INLINE_SEM
    if _INLINE_SEM is None:
        _INLINE_SEM = asyncio.Semaphore(get_settings().thumbnail_inline_concurrency)
    return _INLINE_SEM


async def _require_thumb_scope(
    request: Request,
    response: Response,
    creds: HTTPAuthorizationCredentials | None = Depends(_thumb_bearer),
    api_key: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> PermissionContext:
    """Read-scope guard that ALSO accepts the key via ``?api_key=`` -- an HTML
    ``<img>`` cannot set an Authorization header (same constraint the SSE events
    endpoint handles). Header bearer is preferred for non-browser clients. The
    key is verified exactly as everywhere else (never logged/echoed).

    Returns a :class:`PermissionContext` so ``get_thumb`` can 404 a thumbnail for
    an item outside a scoped session user's read scope (P6-T4) — a browser then
    renders its placeholder, learning nothing about the item's existence."""
    from filearr import grant_cache, rbac, rbac_sql
    from filearr.security import resolve_session_principal

    settings = get_settings()
    if not settings.auth_enabled:
        return PermissionContext(unrestricted=True, action="search_metadata")
    # Bearer / api_key carrier (trusted integration) -> unrestricted (legacy).
    token = creds.credentials if creds is not None else api_key
    if token:
        await _verify_credentials(token, "read", session, request)
        return PermissionContext(unrestricted=True, action="search_metadata")
    # Interactive session cookie (browser <img>).
    principal = await resolve_session_principal(request, response, session)
    if principal is not None:
        from filearr import authx

        if "read" not in authx.scopes_for_role(principal.global_role):
            raise HTTPException(403, "Scope 'read' required")
        if principal.global_role == rbac.Role.ADMIN.value:
            return PermissionContext(unrestricted=True, action="search_metadata")
        role, grants = await grant_cache.load_grants(request, session, principal.id)
        use_ltree = await rbac_sql.path_scope_uses_ltree(session)
        return PermissionContext(
            unrestricted=False,
            action="search_metadata",
            role=role,
            grants=grants,
            use_ltree=use_ltree,
            principal=principal,
        )
    raise HTTPException(401, "Missing bearer token or api_key")


def _thumb_media_type(path: str) -> str:
    """Content-Type from the stored blob's magic bytes. Central-generated thumbs
    are WebP; agent-generated thumbs (P12-T13) are JPEG stored under the same
    ``<key>.webp`` name (no pure-Go lossy WebP encoder under CGO_ENABLED=0), so the
    extension is NOT authoritative — sniff the bytes. Defaults to image/webp."""
    try:
        with open(path, "rb") as fh:  # noqa: ASYNC230 - 12-byte local cache read
            head = fh.read(12)
    except OSError:
        return "image/webp"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/webp"


def _thumb_response(path: str, cache_key: str) -> FileResponse:
    # The URL's bytes never change for a given cache key (content-addressed), so
    # serve fully immutable + a stable ETag; browsers cache for a year and never
    # revalidate. A source change routes to a DIFFERENT key/row, so a stale byte
    # can never be served under a live key. The Content-Type is sniffed (not
    # assumed webp) so an agent-generated JPEG thumbnail serves correctly.
    return FileResponse(
        path,
        media_type=_thumb_media_type(path),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{cache_key}"',
        },
    )


@router.get("/{item_id}/thumb")
async def get_thumb(
    item_id: uuid.UUID,
    tier: str = "grid",
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(_require_thumb_scope),
):
    """Serve an item's WebP thumbnail for ``tier`` (``grid`` | ``preview``).

    ``tier`` is mapped through a STRICT allowlist before it can touch a path
    (research §8 -- no client input beyond the enum ever reaches the filesystem).
    Manifest hit -> ``FileResponse`` with immutable cache headers. Miss -> bounded
    inline generation (preview is always lazy; grid may still be queued). 404 when
    the item has no decodable source (skipped media type / undecodable file)."""
    settings = get_settings()
    if not settings.thumbs_enabled:
        raise HTTPException(404, "Thumbnails are disabled")
    tier_int = th.tier_from_name(tier)
    if tier_int is None:
        raise HTTPException(422, "Invalid tier (expected 'grid' or 'preview')")

    item = await _get_item(session, item_id)  # 404 when the item is gone
    ctx.authorize_item(item)  # 404 when outside a scoped user's read scope

    row = (
        await session.execute(
            select(ThumbnailManifest).where(
                ThumbnailManifest.item_id == item.id,
                ThumbnailManifest.tier == tier_int,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        path = th.abs_path(settings, row.cache_key)
        if os.path.exists(path):  # noqa: ASYNC240 - cheap local cache stat, not loop-blocking
            return _thumb_response(path, row.cache_key)

    # Miss on a VIDEO: NEVER run ffmpeg inline (its latency variance would block
    # the request handler). Enqueue the low-priority thumbs job for this tier and
    # 404 -- the client renders a placeholder and the <img> naturally retries on a
    # later render, by which point the queued frame-grab has landed. Best-effort:
    # a transient enqueue failure just means the next request re-queues.
    if item.media_type == MediaType.video:
        try:
            from filearr.worker import defer_thumb_item

            await defer_thumb_item(str(item.id), tier_int)
        except Exception:  # noqa: BLE001 - enqueue is best-effort (disposable artifact)
            pass
        raise HTTPException(404, "Thumbnail queued; retry shortly")

    # Miss on a cheap in-process source (image/audio cover): generate inline,
    # bounded by the concurrency semaphore.
    from filearr.diskguard import DiskGuardError
    from filearr.tasks.thumbs import generate_and_store

    async with _inline_sem():
        try:
            row = await generate_and_store(session, item, tier_int, settings)
        except DiskGuardError:
            # FIX-11: the cache filesystem is at the critical low-space floor, so
            # inline generation is refused. A thumbnail is disposable — 404 (the
            # client renders a placeholder) rather than 500. Space frees via the
            # monitor's emergency GC; the thumb regenerates on a later request.
            raise HTTPException(404, "No thumbnail available (low disk)") from None
        if row is not None:
            await session.commit()
    if row is None:
        raise HTTPException(404, "No thumbnail available for this item")
    path = th.abs_path(settings, row.cache_key)
    if not os.path.exists(path):  # noqa: ASYNC240 - cheap local cache stat
        raise HTTPException(404, "No thumbnail available for this item")
    return _thumb_response(path, row.cache_key)


@router.patch("/{item_id}", response_model=ItemOut)
async def patch_item(
    item_id: uuid.UUID,
    patch: ItemPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("edit_metadata")),
) -> dict:
    item = await _get_item(session, item_id)
    # 404 if the item is outside the caller's read scope; 403 if readable but the
    # edit_metadata action is denied (404-vs-403 ruling).
    ctx.authorize_item(item, action="edit_metadata")
    changes = patch.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Empty patch")

    for field in ("title", "year", "tags", "external_ids"):
        if field in changes:
            setattr(item, field, changes[field])
    if "user_metadata" in changes and changes["user_metadata"] is not None:
        # P4-T4: type-check custom-field-governed values BEFORE mutating the item
        # (structured 422 on violation; unregistered/non-applicable keys pass).
        _validate_user_metadata_write(
            await _load_custom_field_defs(session), item, changes["user_metadata"]
        )
        # merge semantics: absent keys untouched, explicit null clears a key
        merged = dict(item.user_metadata)
        for k, v in changes["user_metadata"].items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        item.user_metadata = merged

    session.add(
        ItemVersion(item_id=item.id, actor=getattr(request.state, "actor", "ui"), patch=changes)
    )
    await session.commit()
    await session.refresh(item)
    await defer_index_sync([str(item.id)])
    # Return through the same GPS-gated projection as GET (a PATCH response must not
    # leak an unexposed GPS coordinate either).
    library = (
        await session.execute(select(Library).where(Library.id == item.library_id))
    ).scalar_one_or_none()
    share_url, share_source = await _resolve_item_share(session, item, library)
    return _with_native_path(item, library, share_url, share_source)


@router.post("/batch")
async def batch_patch(
    patches: dict[uuid.UUID, ItemPatch],
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("edit_metadata")),
) -> dict:
    results: dict[str, object] = {}
    synced: list[str] = []
    defs = await _load_custom_field_defs(session)
    for item_id, patch in patches.items():
        try:
            item = await _get_item(session, item_id)
            # Per-item RBAC: 404 unreadable / 403 readable-but-denied (recorded
            # per item below, never failing the whole batch).
            ctx.authorize_item(item, action="edit_metadata")
            changes = patch.model_dump(exclude_unset=True)
            # P4-T4: validate custom-field values for this item BEFORE applying,
            # so a rejected item mutates nothing (and records no version row).
            if changes.get("user_metadata"):
                _validate_user_metadata_write(defs, item, changes["user_metadata"])
            for field in ("title", "year", "tags", "external_ids"):
                if field in changes:
                    setattr(item, field, changes[field])
            if changes.get("user_metadata"):
                item.user_metadata = {**item.user_metadata, **changes["user_metadata"]}
            session.add(
                ItemVersion(
                    item_id=item.id, actor=getattr(request.state, "actor", "ui"), patch=changes
                )
            )
            results[str(item_id)] = "ok"
            synced.append(str(item_id))
        except HTTPException as exc:
            # A validation failure carries a structured list detail; keep it
            # machine-readable per item. Other errors (e.g. 404) stay strings.
            if isinstance(exc.detail, list):
                results[str(item_id)] = {"error": "validation", "detail": exc.detail}
            else:
                results[str(item_id)] = f"error: {exc.detail}"
    await session.commit()
    if synced:
        await defer_index_sync(synced)
    return {"results": results}


# --------------------------------------------------------------------------- #
# P3-T10 — duplicate awareness. Copy identity is derived from the scan-time    #
# hashes already stored on ``items`` (no ML, no per-hit Meili queries):        #
#   * content_hash present  -> group by content_hash (whole-file identity);    #
#   * content_hash NULL      -> fall back to (quick_hash, size) among the       #
#     OTHER content-hash-null items (disjoint partition, so the two grouping    #
#     keys never double-count each other).                                     #
# Only ``active`` items count as copies (a trashed tombstone is not a live      #
# duplicate). Both endpoints are read scope.                                    #
# --------------------------------------------------------------------------- #

COPIES_CAP = 50  # max OTHER copies listed by GET /items/{id}/copies


def _copy_native_path(item: Item, library: Library | None) -> str | None:
    """native_prefix + rel_path for a copy row (invariant 3), or None when the
    library maps no native prefix. Mirrors ``_with_native_path``'s join logic."""
    if library is None or not library.native_prefix:
        return None
    sep = "\\" if "\\" in library.native_prefix else "/"
    return library.native_prefix.rstrip(sep) + sep + item.rel_path.replace("/", sep)


def _copy_group_filter(item: Item):
    """The WHERE predicate selecting an item's copy GROUP (self included), plus a
    short ``match`` label. Returns ``(predicate, match)`` or ``(None, "none")``
    when the item carries no usable hash (and therefore has no copies)."""
    if item.content_hash:
        return (Item.content_hash == item.content_hash, "content_hash")
    if item.quick_hash is not None:
        return (
            (Item.content_hash.is_(None))
            & (Item.quick_hash == item.quick_hash)
            & (Item.size == item.size),
            "quick_hash",
        )
    return (None, "none")


@router.get(
    "/{item_id}/copies",
    response_model=CopiesResponse,
)
async def item_copies(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> CopiesResponse:
    """List the OTHER active copies of an item (P3-T10 inline expansion).

    ``count`` is the full group size (this item included) so a caller can render
    "N copies"; ``copies`` excludes self and is capped at ``COPIES_CAP`` with the
    owning library name + native/container path for the copy-path action."""
    item = await _get_item(session, item_id)
    ctx.authorize_item(item)  # 404 if the source item is outside read scope
    predicate, match = _copy_group_filter(item)
    if predicate is None:
        return CopiesResponse(id=item.id, count=1, match="none", capped=False, copies=[])

    base = predicate & (Item.status == "active")
    # RBAC: a copy the caller cannot read must not appear (nor inflate `count`).
    scope_clause = ctx.sql_clause()
    if scope_clause is not None:
        base = base & scope_clause
    total = (
        await session.execute(select(func.count()).select_from(Item).where(base))
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(Item, Library.name)
                .join(Library, Library.id == Item.library_id)
                .where(base & (Item.id != item.id))
                .order_by(Item.last_seen.desc())
                .limit(COPIES_CAP)
            )
        )
        .all()
    )
    # Resolve native_prefix per owning library in ONE grouped query (no per-row
    # async lazy load), then compose each copy's native_path (invariant 3).
    lib_ids = {copy.library_id for copy, _n in rows}
    libs = {
        lib.id: lib
        for lib in (
            await session.execute(select(Library).where(Library.id.in_(lib_ids)))
        )
        .scalars()
        .all()
    } if lib_ids else {}
    copies = [
        ItemCopy(
            id=copy.id,
            library_id=copy.library_id,
            library_name=lib_name,
            rel_path=copy.rel_path,
            path=copy.path,
            native_path=_copy_native_path(copy, libs.get(copy.library_id)),
            size=copy.size,
            last_seen=copy.last_seen,
        )
        for copy, lib_name in rows
    ]

    return CopiesResponse(
        id=item.id,
        count=int(total),
        match=match,
        capped=int(total) - 1 > COPIES_CAP,
        copies=copies,
    )



SIMILAR_STRIP = ("body_text", "_formatted", "_vectors")


def _shape_similar_hit(hit: dict) -> dict:
    """Drop index-side machinery (raw body, formatting, vectors) from a /similar
    hit so the response mirrors a normal search hit's public shape."""
    return {k: v for k, v in hit.items() if k not in SIMILAR_STRIP}


@router.get(
    "/{item_id}/similar",
)
async def item_similar(
    item_id: uuid.UUID,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
    # P6-T4: single resolution gives BOTH the source-item read check AND the
    # deny-aware Meili scope filter (None => admin / API key / auth-off).
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict:
    """Related / near-duplicate items via the item's semantic vector (P3-T9).

    A thin wrapper over Meili's native ``/similar`` endpoint keyed on this item's
    ``userProvided`` vector: excludes sidecars (``is_sidecar = false``) and drops
    the item itself from the results. Returns ``409`` with a clear message when
    semantic search is DISABLED, or when this item has no current-fingerprint
    embedding yet (not embedded / drifted) — a caller can surface "enable semantic
    search" / "backfill pending" rather than an opaque empty list."""
    limit = max(1, min(limit, 50))
    settings = get_settings()
    if not settings.semantic_enabled:
        raise HTTPException(409, "Semantic search is disabled (FILEARR_SEMANTIC_ENABLED)")
    item = await _get_item(session, item_id)
    ctx.authorize_item(item)  # 404 if the source item is outside read scope
    from filearr.tenant_tokens import CompilationRefused

    try:
        scope_filter = ctx.search_filter()
    except CompilationRefused as exc:  # R2: refuse, never coarsen
        raise HTTPException(422, str(exc)) from exc
    if not has_current_embedding(item.metadata_, settings.embedder_config):
        raise HTTPException(
            409, "This item has no current embedding yet (run the embed backfill)"
        )

    from filearr.search import client

    async with client() as c:
        similar_filter = "is_sidecar = false"
        if scope_filter:
            # P6-T3: also constrain "related items" to the caller's granted scopes
            # so /similar can't surface titles from un-granted libraries/paths.
            similar_filter = f"{similar_filter} AND {scope_filter}"
        res = await c.index(settings.meili_index).search_similar_documents(
            str(item_id),
            embedder=DEFAULT_EMBEDDER_NAME,
            limit=limit + 1,  # +1 headroom so excluding self still fills `limit`
            filter=similar_filter,
        )
    hits = [h for h in (res.hits or []) if str(h.get("id")) != str(item_id)][:limit]
    return {"id": str(item_id), "hits": [_shape_similar_hit(h) for h in hits]}


@router.post(
    "/copy-counts",
)
async def copy_counts(
    body: CopyCountsRequest,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict[str, int]:
    """Batch copy-count badge data for a page of search results (P3-T10).

    Body: up to 200 item ids. Returns ``{id: count}`` ONLY for ids whose copy
    group has more than one active member (count > 1) — a single grouped SQL pass
    over the requested ids' hashes, never a per-row Meili query. Ids that don't
    exist, have no hash, or have a unique file are simply absent from the map."""
    ids = list(dict.fromkeys(body.ids))  # de-dupe, preserve order
    if not ids:
        return {}

    # 1) fetch the requested items' identity columns in one query.
    requested = (
        (
            await session.execute(
                select(
                    Item.id, Item.content_hash, Item.quick_hash, Item.size
                ).where(Item.id.in_(ids))
            )
        )
        .all()
    )

    content_hashes = {r.content_hash for r in requested if r.content_hash}
    qh_pairs = {
        (r.quick_hash, r.size)
        for r in requested
        if not r.content_hash and r.quick_hash is not None
    }

    # 2) grouped counts over the content_hash partition (active items only).
    scope_clause = ctx.sql_clause()
    ch_counts: dict[str, int] = {}
    if content_hashes:
        ch_where = (Item.status == "active") & (Item.content_hash.in_(content_hashes))
        if scope_clause is not None:
            ch_where = ch_where & scope_clause
        for ch, cnt in (
            await session.execute(
                select(Item.content_hash, func.count())
                .where(ch_where)
                .group_by(Item.content_hash)
            )
        ).all():
            ch_counts[ch] = int(cnt)

    # 3) grouped counts over the (quick_hash, size) fallback partition — disjoint
    # from the content_hash partition (content_hash IS NULL), so no double count.
    qh_counts: dict[tuple[str, int], int] = {}
    if qh_pairs:
        qh_where = (
            (Item.status == "active")
            & (Item.content_hash.is_(None))
            & (tuple_(Item.quick_hash, Item.size).in_(list(qh_pairs)))
        )
        if scope_clause is not None:
            qh_where = qh_where & scope_clause
        for qh, sz, cnt in (
            await session.execute(
                select(Item.quick_hash, Item.size, func.count())
                .where(qh_where)
                .group_by(Item.quick_hash, Item.size)
            )
        ).all():
            qh_counts[(qh, sz)] = int(cnt)

    out: dict[str, int] = {}
    for r in requested:
        if r.content_hash:
            n = ch_counts.get(r.content_hash, 1)
        elif r.quick_hash is not None:
            n = qh_counts.get((r.quick_hash, r.size), 1)
        else:
            n = 1
        if n > 1:
            out[str(r.id)] = n
    return out
