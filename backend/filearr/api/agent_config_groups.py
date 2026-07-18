"""Agent configuration groups + remote configuration + installer distribution
(Wave 6, W6-D2).

Three admin surfaces, all behind ``FILEARR_AGENTS_ENABLED`` (404 when off) and
``admin`` scope, all audited:

* **Config-group CRUD** — ``/agents/config-groups`` (list/create/get/update/
  delete). A group is a named, reusable remote-configuration bundle (typed
  ``settings`` — :mod:`filearr.agent_config`) assigned to many agents. NULL
  ``agents.config_group_id`` is the built-in default; a "default" group is NOT
  special-cased. Deleting a group with members lets the ON DELETE SET NULL FK
  fall them back to NULL (the audit records the member count).

* **Assignment** — ``PUT /agents/{id}/config-group`` sets/clears an agent's
  group (matches the agents API's dedicated-mutation convention — the agents
  surface uses POST/DELETE/PUT sub-resources, not a general PATCH).

* **Installer distribution** — ``POST /agents/installer-config`` mints an
  enrollment token (existing machinery) and returns the COMPLETE sidecar JSON the
  W6-D1 console agent consumes, plus token metadata + per-OS install hints. The
  UI (W6-D4) renders/downloads it (FROZEN response contract — see
  :class:`InstallerConfigOut`).

The remote-configuration DELIVERY half (merging a group's settings into the
agent policy doc under a new ``group`` section, with ETag invalidation on edit)
lives in :mod:`filearr.api.agent_policies` + :func:`agent_config.merge_group_into_policy`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, agentsync, audit
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentConfigGroup
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class ConfigGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    # Typed at the request boundary as a dict so a non-object body is a 422 at
    # parse time; agent_config.validate_settings runs the known-key gate after.
    settings: dict[str, Any] = Field(default_factory=dict)


class ConfigGroupUpdateIn(BaseModel):
    # All optional (PATCH-style partial update); an omitted field is left as-is.
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    settings: dict[str, Any] | None = None


class ConfigGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    settings: dict[str, Any]
    member_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: AgentConfigGroup, member_count: int) -> ConfigGroupOut:
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            settings=row.settings or {},
            member_count=member_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AssignConfigGroupIn(BaseModel):
    # NULL clears the assignment (fall back to built-in defaults).
    config_group_id: uuid.UUID | None = None


class InstallerConfigIn(BaseModel):
    central_url_override: str | None = Field(default=None, max_length=2048)
    agent_name: str | None = Field(default=None, max_length=255)
    config_group_id: uuid.UUID | None = None
    log_level: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class InstallerSidecar(BaseModel):
    """The COMPLETE sidecar the W6-D1 console agent consumes (written as
    ``filearr-agent.json``)."""

    central_url: str
    enrollment_token: str  # raw, show-once (rides the mint's show-once contract)
    agent_name: str | None
    config_group: str | None  # group NAME (matches register's string field)
    log_level: str | None


class InstallHint(BaseModel):
    windows: str
    linux: str
    macos: str


class InstallerConfigOut(BaseModel):
    """FROZEN CONTRACT for W6-D4. ``sidecar`` is the file the agent consumes;
    ``token_hash``/``expires_at`` let the UI show/revoke the token; ``install_hint``
    carries per-OS one-line install commands referencing the P5-T7 release-artifact
    download path + ``filearr-agent install --config filearr-agent.json``."""

    sidecar: InstallerSidecar
    token_hash: str
    expires_at: datetime
    install_hint: InstallHint


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def _member_count(session: AsyncSession, group_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Agent).where(
                Agent.config_group_id == group_id
            )
        )
    ).scalar_one()


def _validate_settings_or_422(settings: Any) -> None:
    try:
        agent_config.validate_settings(settings)
    except agent_config.GroupSettingsValidationError as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)
        ) from err


# --------------------------------------------------------------------------- #
# Config-group CRUD (admin)                                                    #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/config-groups",
    response_model=list[ConfigGroupOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_config_groups(
    session: AsyncSession = Depends(get_session),
) -> list[ConfigGroupOut]:
    """Every config group (newest first) with its current member count."""
    rows = (
        await session.execute(
            select(AgentConfigGroup).order_by(AgentConfigGroup.created_at.desc())
        )
    ).scalars().all()
    # One grouped count query, then attribute per group (avoids N+1).
    counts = dict(
        (
            await session.execute(
                select(Agent.config_group_id, func.count())
                .where(Agent.config_group_id.is_not(None))
                .group_by(Agent.config_group_id)
            )
        ).all()
    )
    return [ConfigGroupOut.of(r, counts.get(r.id, 0)) for r in rows]


@router.post(
    "/agents/config-groups",
    response_model=ConfigGroupOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def create_config_group(
    body: ConfigGroupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    """Create a config group. ``settings`` is validated (422 on unknown top-level
    key / bad preset / bad regex / bad cron / oversize) and stored verbatim. A
    duplicate ``name`` is a 409 (``name`` is UNIQUE)."""
    _validate_settings_or_422(body.settings)
    row = AgentConfigGroup(
        name=body.name,
        description=body.description,
        settings=body.settings,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as err:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"config group name already exists: {body.name!r}"
        ) from err
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(row.id), "name": row.name},
    )
    return ConfigGroupOut.of(row, 0)


@router.get(
    "/agents/config-groups/{group_id}",
    response_model=ConfigGroupOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def get_config_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    return ConfigGroupOut.of(row, await _member_count(session, row.id))


@router.patch(
    "/agents/config-groups/{group_id}",
    response_model=ConfigGroupOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def update_config_group(
    group_id: uuid.UUID,
    body: ConfigGroupUpdateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    """Partial update. A supplied ``settings`` is re-validated (422) and REPLACES
    the stored object (settings are not deep-merged — an edit is authored whole).
    A duplicate ``name`` is a 409. The edit bumps ``updated_at``, which invalidates
    every member agent's cached policy (the ETag folds in the group tag)."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.settings is not None:
        _validate_settings_or_422(body.settings)
        row.settings = body.settings
    try:
        await session.commit()
    except IntegrityError as err:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "config group name already exists"
        ) from err
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(row.id), "name": row.name},
    )
    return ConfigGroupOut.of(row, await _member_count(session, row.id))


@router.delete(
    "/agents/config-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def delete_config_group(
    group_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a config group. Members (if any) fall back to NULL via the ON DELETE
    SET NULL FK (built-in defaults); the audit records how many did. Always
    allowed — a group is never load-bearing enough to block deletion."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    members = await _member_count(session, row.id)
    name = row.name
    await session.delete(row)
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(group_id), "name": name, "members_reset": members},
    )


# --------------------------------------------------------------------------- #
# Assignment (admin) — PUT /agents/{id}/config-group                           #
# --------------------------------------------------------------------------- #
@router.put(
    "/agents/{agent_id}/config-group",
    response_model=ConfigGroupOut | None,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def assign_config_group(
    agent_id: uuid.UUID,
    body: AssignConfigGroupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut | None:
    """Assign (or clear, with ``config_group_id: null``) an agent's config group.
    404 for an unknown agent or an unknown target group. Returns the newly-assigned
    group (or ``null`` when cleared). Audited (old → new group)."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    old = agent.config_group_id
    group: AgentConfigGroup | None = None
    if body.config_group_id is not None:
        group = await session.get(AgentConfigGroup, body.config_group_id)
        if group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    agent.config_group_id = body.config_group_id
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_ASSIGNED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "agent_id": str(agent_id),
            "old_group_id": str(old) if old else None,
            "new_group_id": str(body.config_group_id) if body.config_group_id else None,
        },
    )
    if group is None:
        return None
    return ConfigGroupOut.of(group, await _member_count(session, group.id))


# --------------------------------------------------------------------------- #
# Console installer distribution (admin) — POST /agents/installer-config       #
# --------------------------------------------------------------------------- #
def _install_hint(central_url: str) -> InstallHint:
    """Per-OS one-line install commands. Each downloads the platform artifact from
    the P5-T7 agent release-artifact path
    (``/api/v1/agents/{agent_id}/releases/{version}/artifacts/{filename}``) and
    runs ``filearr-agent install --config filearr-agent.json``. ``{agent_id}`` /
    ``{version}`` are placeholders the operator fills from the fleet console after
    enrollment (the artifact path is agent-authenticated)."""
    base = central_url.rstrip("/")
    art = f"{base}/api/v1/agents/{{agent_id}}/releases/{{version}}/artifacts"
    return InstallHint(
        windows=(
            f"Invoke-WebRequest {art}/filearr-agent-windows-amd64.exe "
            "-OutFile filearr-agent.exe; "
            ".\\filearr-agent.exe install --config filearr-agent.json"
        ),
        linux=(
            f"curl -fsSL {art}/filearr-agent-linux-amd64 -o filearr-agent && "
            "chmod +x filearr-agent && "
            "./filearr-agent install --config filearr-agent.json"
        ),
        macos=(
            f"curl -fsSL {art}/filearr-agent-darwin-arm64 -o filearr-agent && "
            "chmod +x filearr-agent && "
            "./filearr-agent install --config filearr-agent.json"
        ),
    )


@router.post(
    "/agents/installer-config",
    response_model=InstallerConfigOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def issue_installer_config(
    body: InstallerConfigIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> InstallerConfigOut:
    """Mint an enrollment token (existing machinery) and return the COMPLETE
    sidecar the W6-D1 console agent consumes, plus token metadata + per-OS install
    hints (FROZEN contract, :class:`InstallerConfigOut`).

    ``central_url`` = ``central_url_override`` or the request base URL.
    ``config_group_id`` (if given) must exist (422 otherwise) and is emitted in the
    sidecar by NAME (the agent later presents it to ``/agents/register``).
    ``log_level`` (if given) must be a known level (422). Audited by token hash +
    config group (NEVER the raw token)."""
    settings = get_settings()

    if body.log_level is not None and body.log_level not in agent_config.LOG_LEVELS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"log_level must be one of {list(agent_config.LOG_LEVELS)}",
        )

    group_name: str | None = None
    if body.config_group_id is not None:
        group = await session.get(AgentConfigGroup, body.config_group_id)
        if group is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "no such config group"
            )
        group_name = group.name

    central_url = (
        body.central_url_override.rstrip("/")
        if body.central_url_override
        else str(request.base_url).rstrip("/")
    )

    ttl_seconds = body.ttl_seconds or (settings.enrollment_token_ttl_minutes * 60)
    raw, tok = await agentsync.mint_enrollment_token(
        session, rollout_group="default", ttl_seconds=ttl_seconds
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_INSTALLER_CONFIG_ISSUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "token_hash": tok.token_hash,
            "config_group": group_name,
            "agent_name": body.agent_name,
        },
    )
    return InstallerConfigOut(
        sidecar=InstallerSidecar(
            central_url=central_url,
            enrollment_token=raw,
            agent_name=body.agent_name,
            config_group=group_name,
            log_level=body.log_level,
        ),
        token_hash=tok.token_hash,
        expires_at=tok.expires_at,
        install_hint=_install_hint(central_url),
    )
