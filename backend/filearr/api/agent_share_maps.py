"""P10-T12 — central ``agent_share_maps`` fallback (admin CRUD, user-mandated).

The per-AGENT equivalent of OPS-T7's deploy-time ``share-map.json`` (which covers
the CENTRAL server's own mounts). When an agent can't self-report a network
``ShareHint`` (P10-T11 is best-effort, R1), an operator declares centrally how a
path on agent X maps to a network share, so an agent-hosted item still renders a
network-open link. Resolution reuses the ONE canonical longest-``local_prefix``-
wins resolver (:func:`filearr.share_map.resolve_for_agent` /
:func:`filearr.transfers._select_mapping`, R4); an agent-scoped rule outranks a
global (``agent_id IS NULL`` = any agent) one of equal prefix length.

The whole surface is behind the ``FILEARR_AGENTS_ENABLED`` 404 gate (same as the
enrollment + command planes). Mutations require the ``admin`` scope (this is
operator configuration, like every other admin surface); reads require ``read``.
Every mutation is audited via ``security_events`` unconditionally.

Validation (brief): ``local_prefix`` is normalised (trailing separators trimmed);
``share_prefix`` must be a UNC (``\\host\share``), a POSIX mount (``/Volumes/…``),
or a URL in the scheme allowlist ``smb/sftp/ftp/nfs/webdav`` — any other scheme,
an unclassifiable prefix, or a URL carrying **credentials** (``user[:pass]@host``)
is rejected 422 (a share map is credential-free by construction). A duplicate
``(agent_id, library_id, local_prefix)`` is a 409 (DB-backstopped by
``uq_agent_share_maps_scope_prefix``).

WIRE-UP NOTE (P10-T2): items are not agent-owned until ``items.source_agent_id``
gains its ``agents`` FK (P10-T2); once it does, the item-display path consults an
agent ``ShareHint`` first (when present, R1) then :func:`resolve_for_agent` over
these rows. This module + :func:`filearr.share_map.resolve_for_agent` are that
seam; only the item-level dispatch awaits P10-T2.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.api.agents import require_agents_enabled
from filearr.db import get_session
from filearr.models import Agent, AgentShareMap, Library
from filearr.security import require_scope
from filearr.share_map import ShareLocation, resolve_for_agent
from filearr.transfers import ShareMapping, classify_prefix

router = APIRouter()

# URL schemes an operator may point a share map at (brief allowlist). UNC and a
# POSIX mount (``/Volumes/…``) are separately allowed (doc DDL comment).
_ALLOWED_URL_SCHEMES = frozenset({"smb", "sftp", "ftp", "nfs", "webdav"})
_PREFIX_MAX = 4096


# --------------------------------------------------------------------------- #
# Validation helpers                                                           #
# --------------------------------------------------------------------------- #
def _normalize_local_prefix(raw: str) -> str:
    """Trim whitespace + trailing path separators so ``/media/`` and ``/media``
    are one prefix (prevents trivial duplicates). Leading form is preserved
    (``C:\\media`` or ``/media``); the resolver is separator-safe either way."""
    p = (raw or "").strip()
    if not p:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "local_prefix is empty")
    if len(p) > _PREFIX_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "local_prefix too long")
    stripped = p.rstrip("/\\")
    return stripped or p  # never collapse a bare root ("/" / "\\") to empty


def _url_has_credentials(url: str) -> bool:
    """True if a ``scheme://`` URL carries userinfo (``user[:pass]@host``) — a
    share map must be credential-free, so such a value is rejected."""
    i = url.find("://")
    if i < 0:
        return False
    authority = url[i + 3 :].split("/", 1)[0]
    return "@" in authority


def _validate_share_prefix(raw: str) -> str:
    """Validate + return a stripped ``share_prefix``: a UNC, a POSIX mount, or an
    allowlisted URL scheme, never with embedded credentials. 422 otherwise."""
    p = (raw or "").strip()
    if not p:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "share_prefix is empty")
    if len(p) > _PREFIX_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "share_prefix too long")
    kind = classify_prefix(p)
    if kind == "url":
        if _url_has_credentials(p):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "share_prefix must not contain credentials",
            )
        scheme = p[: p.find("://")].lower()
        if scheme not in _ALLOWED_URL_SCHEMES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"share_prefix scheme '{scheme}' not allowed "
                f"(allowed: {', '.join(sorted(_ALLOWED_URL_SCHEMES))})",
            )
        return p
    if kind in ("unc", "posix"):
        return p
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "share_prefix must be a UNC (\\\\host\\share), an smb/sftp/ftp/nfs/webdav "
        "URL, or a POSIX mount (/Volumes/...)",
    )


def _validate_unc(raw: str | None) -> str | None:
    """An explicit ``unc`` counterpart, when supplied, must be a real UNC path."""
    if raw is None:
        return None
    p = raw.strip()
    if not p:
        return None
    if classify_prefix(p) != "unc":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "unc must be a \\\\host\\share path"
        )
    return p


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class ShareMapCreateIn(BaseModel):
    library_id: uuid.UUID | None = None
    local_prefix: str = Field(min_length=1)
    share_prefix: str = Field(min_length=1)
    unc: str | None = None
    storage_type: str | None = Field(default=None, max_length=64)
    host: str | None = Field(default=None, max_length=255)


class ShareMapUpdateIn(BaseModel):
    library_id: uuid.UUID | None = None
    local_prefix: str | None = None
    share_prefix: str | None = None
    unc: str | None = None
    storage_type: str | None = Field(default=None, max_length=64)
    host: str | None = Field(default=None, max_length=255)


class ShareLocationOut(BaseModel):
    url: str | None = None
    unc: str | None = None


class ShareMapOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID | None
    library_id: uuid.UUID | None
    local_prefix: str
    share_prefix: str
    unc: str | None
    storage_type: str | None
    host: str | None
    created_at: datetime
    updated_at: datetime
    # A convenience preview of how ``local_prefix`` itself resolves (both OS
    # formats), so the UI can show the effective link without re-deriving it.
    location: ShareLocationOut

    @classmethod
    def of(cls, m: AgentShareMap) -> ShareMapOut:
        loc = resolve_for_agent(
            [
                ShareMapping(
                    local_prefix=m.local_prefix,
                    share_prefix=m.share_prefix,
                    agent_id=str(m.agent_id) if m.agent_id else None,
                    library_id=str(m.library_id) if m.library_id else None,
                    unc=m.unc,
                )
            ],
            str(m.agent_id) if m.agent_id else None,
            m.local_prefix,
        ) or ShareLocation()
        return cls(
            id=m.id,
            agent_id=m.agent_id,
            library_id=m.library_id,
            local_prefix=m.local_prefix,
            share_prefix=m.share_prefix,
            unc=m.unc,
            storage_type=m.storage_type,
            host=m.host,
            created_at=m.created_at,
            updated_at=m.updated_at,
            location=ShareLocationOut(url=loc.url, unc=loc.unc),
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def _require_library(session: AsyncSession, library_id: uuid.UUID | None) -> None:
    if library_id is None:
        return
    if await session.get(Library, library_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such library")


async def _dup_exists(
    session: AsyncSession,
    agent_id: uuid.UUID | None,
    library_id: uuid.UUID | None,
    local_prefix: str,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    q = select(AgentShareMap.id).where(
        AgentShareMap.agent_id.is_(None)
        if agent_id is None
        else AgentShareMap.agent_id == agent_id,
        AgentShareMap.library_id.is_(None)
        if library_id is None
        else AgentShareMap.library_id == library_id,
        AgentShareMap.local_prefix == local_prefix,
    )
    if exclude_id is not None:
        q = q.where(AgentShareMap.id != exclude_id)
    return (await session.execute(q.limit(1))).first() is not None


async def _load(session: AsyncSession, map_id: uuid.UUID) -> AgentShareMap:
    m = await session.get(AgentShareMap, map_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such share map")
    return m


# --------------------------------------------------------------------------- #
# CRUD                                                                         #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/share-maps",
    response_model=ShareMapOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def create_share_map(
    agent_id: uuid.UUID,
    body: ShareMapCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ShareMapOut:
    """Create one share mapping scoped to ``agent_id`` (which must exist). The
    ``local_prefix`` is normalised, the ``share_prefix`` is validated against the
    scheme allowlist + credential ban, and a duplicate
    ``(agent_id, library_id, local_prefix)`` is a 409. Audited."""
    if await session.get(Agent, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    await _require_library(session, body.library_id)
    local_prefix = _normalize_local_prefix(body.local_prefix)
    share_prefix = _validate_share_prefix(body.share_prefix)
    unc = _validate_unc(body.unc)
    if await _dup_exists(session, agent_id, body.library_id, local_prefix):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a mapping for this prefix already exists"
        )
    m = AgentShareMap(
        agent_id=agent_id,
        library_id=body.library_id,
        local_prefix=local_prefix,
        share_prefix=share_prefix,
        unc=unc,
        storage_type=body.storage_type,
        host=body.host,
    )
    session.add(m)
    try:
        await session.commit()
    except IntegrityError:  # unique backstop (race)
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a mapping for this prefix already exists"
        ) from None
    await session.refresh(m)
    await audit.emit(
        audit.AGENT_SHARE_MAP_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "share_map_id": str(m.id),
            "agent_id": str(agent_id),
            "library_id": str(body.library_id) if body.library_id else None,
            "local_prefix": local_prefix,
            "share_prefix": share_prefix,
        },
    )
    return ShareMapOut.of(m)


@router.get(
    "/agent-share-maps",
    response_model=list[ShareMapOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def list_share_maps(
    session: AsyncSession = Depends(get_session),
    agent_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    scope: Literal["all", "global"] | None = None,
    limit: int = 200,
) -> list[ShareMapOut]:
    """List share maps, filterable by ``agent_id`` / ``library_id``.
    ``scope=global`` returns only ``agent_id IS NULL`` (any-agent) rules.
    Newest-first."""
    limit = max(1, min(limit, 500))
    q = select(AgentShareMap).order_by(AgentShareMap.id.desc()).limit(limit)
    if scope == "global":
        q = q.where(AgentShareMap.agent_id.is_(None))
    elif agent_id is not None:
        q = q.where(AgentShareMap.agent_id == agent_id)
    if library_id is not None:
        q = q.where(AgentShareMap.library_id == library_id)
    rows = (await session.execute(q)).scalars().all()
    return [ShareMapOut.of(m) for m in rows]


@router.get(
    "/agent-share-maps/{map_id}",
    response_model=ShareMapOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def get_share_map(
    map_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> ShareMapOut:
    return ShareMapOut.of(await _load(session, map_id))


@router.patch(
    "/agent-share-maps/{map_id}",
    response_model=ShareMapOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def update_share_map(
    map_id: uuid.UUID,
    body: ShareMapUpdateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ShareMapOut:
    """Edit a share map (re-validated + dup-checked exactly like create). Audited."""
    m = await _load(session, map_id)
    fields = body.model_dump(exclude_unset=True)
    new_library = fields["library_id"] if "library_id" in fields else m.library_id
    new_local = (
        _normalize_local_prefix(fields["local_prefix"])
        if "local_prefix" in fields
        else m.local_prefix
    )
    if "library_id" in fields:
        await _require_library(session, new_library)
    if "share_prefix" in fields:
        m.share_prefix = _validate_share_prefix(fields["share_prefix"])
    if "unc" in fields:
        m.unc = _validate_unc(fields["unc"])
    if "storage_type" in fields:
        m.storage_type = fields["storage_type"]
    if "host" in fields:
        m.host = fields["host"]
    if ("local_prefix" in fields or "library_id" in fields) and await _dup_exists(
        session, m.agent_id, new_library, new_local, exclude_id=m.id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a mapping for this prefix already exists"
        )
    m.local_prefix = new_local
    m.library_id = new_library
    m.updated_at = datetime.now(UTC)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a mapping for this prefix already exists"
        ) from None
    await session.refresh(m)
    await audit.emit(
        audit.AGENT_SHARE_MAP_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "share_map_id": str(map_id),
            "agent_id": str(m.agent_id) if m.agent_id else None,
        },
    )
    return ShareMapOut.of(m)


@router.delete(
    "/agent-share-maps/{map_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def delete_share_map(
    map_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    m = await _load(session, map_id)
    agent_id = m.agent_id
    await session.delete(m)
    await session.commit()
    await audit.emit(
        audit.AGENT_SHARE_MAP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "share_map_id": str(map_id),
            "agent_id": str(agent_id) if agent_id else None,
        },
    )
