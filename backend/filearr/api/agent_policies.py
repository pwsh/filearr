"""Agent config/policy push surface (Phase 5, P5-T6).

Two planes, both behind the ``FILEARR_AGENTS_ENABLED`` gate (404 when off):

* **Agent plane** — ``GET /agents/{agent_id}/policy``. Interim agent bearer auth
  (the bound ``cert_fingerprint``; mTLS replaces it — same seam as every other
  agent endpoint, reusing :func:`agent_commands._authenticate_agent`). Resolves
  the EFFECTIVE policy (most-specific-wins: agent > group > global) and serves it
  with a strong ``ETag: "<scope>/<version>"`` supporting ``If-None-Match`` →
  ``304``. The reliable pull path (research §6.2); an agent never 404s here (no
  policy anywhere → ``"none/0"`` + ``{}``). ``?applied=<int>`` reports the version
  the agent has applied (stamps ``agents.policy_version_applied`` + ``last_seen_at``).

* **Admin plane** — ``PUT /agent-policies/{scope}`` (write a new version),
  ``GET /agent-policies`` (current row per scope), ``GET /agent-policies/{scope}/
  history``. ``admin`` scope, audited (``agent_policy_updated`` — scope + version
  only, never the body). Append-only: a write inserts a new row at
  ``version = prior scope max + 1``; old rows are never mutated (§6.3).

mTLS is the only integrity layer for the config channel (R4 — no payload signing
in v3; revisit trigger is phase-6 multi-author RBAC policy).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, audit, taxonomy
from filearr import policy as policy_mod
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentConfigGroup, PolicyVersion
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class PolicyWriteIn(BaseModel):
    # `policy` typed as a dict so a non-object body is a 422 at request-parse time
    # (the "reject non-object policy" rule); the known-key validation runs after.
    policy: dict[str, Any] = Field(default_factory=dict)


class PolicyRowOut(BaseModel):
    id: uuid.UUID
    scope: str
    scope_type: str
    scope_id: str | None
    version: int
    policy: dict[str, Any]
    actor: str | None
    created_at: datetime

    @classmethod
    def of(cls, row: PolicyVersion) -> PolicyRowOut:
        return cls(
            id=row.id,
            scope=policy_mod.scope_string(row.scope_type, row.scope_id),
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            version=row.version,
            policy=row.policy or {},
            actor=row.actor,
            created_at=row.created_at,
        )


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """True if the ``If-None-Match`` header matches ``etag`` (comma list or ``*``)."""
    tokens = [t.strip() for t in if_none_match.split(",")]
    return "*" in tokens or etag in tokens


# --------------------------------------------------------------------------- #
# Agent plane — GET /agents/{id}/policy (bearer; ETag/If-None-Match)            #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/{agent_id}/policy",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_agent_policy(
    agent_id: uuid.UUID,
    request: Request,
    applied: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve ``agent``'s effective policy (agent>group>global; no merging), with
    the agent's config group (W6-D2) merged in under a top-level ``group`` section.

    ``200 {"scope","version","policy"}`` with ``ETag: "<scope>/<version>"`` (or
    ``"<scope>/<version>/g:<tag>"`` when a config group is assigned — the group's
    ``updated_at`` feeds ``<tag>`` so any group edit invalidates the agent's
    cache); a matching ``If-None-Match`` → ``304`` (empty body, ETag still
    present). No policy rows anywhere → ``200 {"scope":"none","version":0,
    "policy":{}}`` (``"none/0"``) — an agent must never 404 here.

    **Precedence** (most-specific wins): per-agent explicit policy keys (the
    resolved ``agent`` > ``group[rollout_group]`` > ``global`` row) > config-group
    settings > agent-side defaults. Config-group settings ride under ``group``;
    a NULL config group adds no ``group`` section, and an operator-authored
    top-level ``group`` policy key wins over the config group (never clobbered).
    Additive: current binaries that ignore ``group`` are unaffected.

    ``?applied=`` stamps the reported-applied version + ``last_seen_at`` (the agent
    is demonstrably alive, like the poll/replication endpoints)."""
    agent = await _authenticate_agent(session, agent_id, request)
    now = datetime.now(UTC)
    agent.last_seen_at = now
    if applied is not None:
        agent.policy_version_applied = applied
    await session.commit()

    scope, version, pol = await policy_mod.resolve_effective_policy(session, agent)

    # W6-D2: fold the agent's config group into the doc + ETag. NULL group → the
    # ETag stays the pre-W6 "<scope>/<version>" form (backward compatible).
    group = (
        await session.get(AgentConfigGroup, agent.config_group_id)
        if agent.config_group_id is not None
        else None
    )
    pol = agent_config.merge_group_into_policy(pol, group)
    group_tag = agent_config.group_etag_tag(group)

    # W8-E: surface the central File Extension Similarity Taxonomy version so a
    # taxonomy edit invalidates the agent's policy cache. The version rides the
    # policy body (an additive, computed key the agent reads to version-gate its
    # compact-taxonomy fetch) AND the ETag (a bump forces a 200, not a 304, even
    # when scope/version are unchanged). Central always sets the authoritative
    # value — an operator-authored ``taxonomy_version`` key does not win.
    tax_version = await taxonomy.current_version(session)
    pol = {**pol, "taxonomy_version": tax_version}
    etag_core = f"{scope}/{version}"
    if group_tag:
        etag_core += f"/g:{group_tag}"
    etag = f'"{etag_core}/t:{tax_version}"'

    inm = request.headers.get("if-none-match")
    if inm and _etag_matches(inm, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return JSONResponse(
        {"scope": scope, "version": version, "policy": pol},
        headers={"ETag": etag},
    )


# --------------------------------------------------------------------------- #
# Agent plane — GET /agents/{id}/taxonomy (bearer; compact resolution payload)  #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/{agent_id}/taxonomy",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_agent_taxonomy(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the COMPACT File Extension Similarity Taxonomy an agent resolves
    against locally (W8-E) — NOT the admin ``GET /taxonomy`` tree.

    The agent fetches this VERSION-GATED: after a policy poll surfaces a newer
    ``taxonomy_version`` than the agent's cached snapshot, it pulls this payload
    once and persists it. Because it is version-gated, shipping the full
    ~1271-entry ``ext_to_group`` map each fetch is fine. The payload is exactly
    :meth:`filearr.taxonomy.Taxonomy.agent_payload` — flat lookup maps plus the
    ``primary_categories`` sidecar-parent set (the categories with an extractor).

    Agent-plane auth (the same interim cert-fingerprint bearer / mTLS-header seam
    as the policy + replication endpoints); 404 when the agents feature is off."""
    await _authenticate_agent(session, agent_id, request)
    payload = await taxonomy.agent_payload(session)
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# Admin plane — write / list / history (admin scope, audited)                  #
# --------------------------------------------------------------------------- #
@router.put(
    "/agent-policies/{scope}",
    response_model=PolicyRowOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def put_policy(
    scope: str,
    body: PolicyWriteIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PolicyRowOut:
    """Write a NEW policy version for ``scope`` (``global`` | ``group:<name>`` |
    ``agent:<uuid>``). Append-only: inserts at ``version = prior scope max + 1``;
    existing rows are never mutated. 422 malformed scope / unknown agent uuid /
    policy fails validation; 413 oversize. Audited (scope + version only)."""
    try:
        scope_type, scope_id = policy_mod.parse_scope(scope)
    except policy_mod.ScopeError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err

    # An agent-scoped policy must target a real agent (422 otherwise).
    if scope_type == "agent":
        agent = await session.get(Agent, uuid.UUID(scope_id))
        if agent is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "no such agent for scope"
            )

    settings = get_settings()
    if policy_mod.policy_json_len(body.policy) > settings.agent_policy_max_bytes:
        raise HTTPException(413, "policy too large")
    try:
        policy_mod.validate_policy(body.policy)
    except policy_mod.PolicyValidationError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err

    version = await policy_mod.next_version(session, scope_type, scope_id)
    row = PolicyVersion(
        scope_type=scope_type,
        scope_id=scope_id,
        version=version,
        policy=body.policy,  # stored VERBATIM (unknown keys preserved)
        actor=audit.actor_id(request),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_POLICY_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "scope": policy_mod.scope_string(scope_type, scope_id),
            "version": version,
        },
    )
    return PolicyRowOut.of(row)


@router.get(
    "/agent-policies",
    response_model=list[PolicyRowOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_current_policies(
    session: AsyncSession = Depends(get_session),
) -> list[PolicyRowOut]:
    """The CURRENT (max-version) row for every scope that has one."""
    q = (
        select(PolicyVersion)
        .distinct(PolicyVersion.scope_type, PolicyVersion.scope_id)
        .order_by(
            PolicyVersion.scope_type,
            PolicyVersion.scope_id,
            PolicyVersion.version.desc(),
        )
    )
    rows = (await session.execute(q)).scalars().all()
    return [PolicyRowOut.of(r) for r in rows]


@router.get(
    "/agent-policies/{scope}/history",
    response_model=list[PolicyRowOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def policy_history(
    scope: str,
    session: AsyncSession = Depends(get_session),
    before: int | None = Query(default=None),
    limit: int = Query(default=100),
) -> list[PolicyRowOut]:
    """Version history for one scope, newest-first (keyset by ``version`` desc;
    ``before`` = the last version of the previous page). Cap 100."""
    try:
        scope_type, scope_id = policy_mod.parse_scope(scope)
    except policy_mod.ScopeError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
    limit = max(1, min(limit, 100))
    q = select(PolicyVersion).where(
        PolicyVersion.scope_type == scope_type,
        PolicyVersion.scope_id.is_(scope_id)
        if scope_id is None
        else PolicyVersion.scope_id == scope_id,
    )
    if before is not None:
        q = q.where(PolicyVersion.version < before)
    q = q.order_by(PolicyVersion.version.desc()).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    return [PolicyRowOut.of(r) for r in rows]
