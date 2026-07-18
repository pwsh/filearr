"""Distributed-agent enrollment surface (Phase 5, P5-T1).

The central-side fleet trust root: minting single-use short-TTL enrollment
tokens, the register-FIRST handshake (R3 — server assigns the authoritative
``agents.id`` before any cert exists), the pending→active cert-fingerprint
binding seam, the fleet console list, and the application-layer revoke kill
switch (research §1.4). Every mutation is audited via ``security_events``.

Auth model (two distinct planes):

* **Operator/admin plane** — token mint/list/revoke, agent list/revoke —
  requires the ``admin`` scope (RBAC-enforced, consistent with every other
  admin surface). These are the console actions.
* **Agent plane** — ``/register`` and ``/certificate`` — is NOT API-key gated:
  the enrollment TOKEN (register) and the one-time enroll SECRET (certificate)
  ARE the credentials, exactly as an unenrolled machine has no API key yet. In
  v3 these ride the enrollment/mTLS channel; P5-T2 hardens ``/certificate``
  behind the freshly-minted client cert. Both still require the feature to be
  enabled.

The whole surface is gated by ``FILEARR_AGENTS_ENABLED`` (default off): a
single-node deploy that never enables agents sees a 404 here and no console
panel. The tables exist regardless (empty), so enabling later needs no
migration. Actual CA signing/renewal is the agent↔step-ca flow (P5-T2, gated by
the P5-T2a integration spike); this module only brokers identity + bootstrap
info.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, audit
from filearr.agentsync import EnrollmentError
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, EnrollmentToken, Item, Library
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Feature gate                                                                 #
# --------------------------------------------------------------------------- #
def require_agents_enabled() -> None:
    """404 the whole surface unless ``FILEARR_AGENTS_ENABLED`` is set. Agents are
    a v3 opt-in; a single-node deploy is unaffected."""
    if not get_settings().agents_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "distributed agents disabled")


# Map an EnrollmentError.reason onto an HTTP status.
_AUTH_REASONS = {"unknown_token", "consumed", "expired", "bad_secret"}
_CONFLICT_REASONS = {"already_bound"}


def _enrollment_http(err: EnrollmentError) -> HTTPException:
    if err.reason in _AUTH_REASONS:
        return HTTPException(status.HTTP_401_UNAUTHORIZED, str(err))
    if err.reason in _CONFLICT_REASONS:
        return HTTPException(status.HTTP_409_CONFLICT, str(err))
    if err.reason in {"unknown_agent"}:
        return HTTPException(status.HTTP_404_NOT_FOUND, str(err))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class TokenMintIn(BaseModel):
    rollout_group: str = Field(default="default", min_length=1, max_length=128)
    # Override the default TTL (minutes); clamped to a sane minutes-to-hours band.
    ttl_minutes: int | None = Field(default=None, ge=1, le=1440)


class TokenMintOut(BaseModel):
    """The ONLY response that ever carries the raw token — shown once."""

    token: str  # raw, show-once, never persisted
    token_hash: str
    rollout_group: str
    expires_at: datetime


class TokenOut(BaseModel):
    token_hash: str
    rollout_group: str
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by: uuid.UUID | None
    created_at: datetime
    status: str  # active | consumed | expired


class RegisterIn(BaseModel):
    token: str = Field(min_length=1)
    hostname: str = Field(min_length=1, max_length=255)
    platform: str  # windows | macos | linux (validated in agentsync)
    name: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=64)
    # W6-D2: config group by NAME (from the installer sidecar). Resolved to
    # config_group_id at register; an unknown name is fail-safe (NULL group +
    # a warning in the response), never a registration failure.
    config_group: str | None = Field(default=None, max_length=128)


class CaBootstrap(BaseModel):
    """Public pinning material the agent needs to reach the CA (never a secret)."""

    url: str
    fingerprint: str
    provisioner: str
    cert_ttl_hours: int


class RegisterOut(BaseModel):
    agent_id: uuid.UUID
    rollout_group: str
    status: str  # 'pending' — awaiting cert binding
    # One-time secret the agent presents to POST /certificate after the CA signs.
    enroll_secret: str
    ca: CaBootstrap
    # P5-T2: scoped step-ca JWK one-time token the agent exchanges DIRECTLY with
    # the CA's /1.0/sign to obtain its client cert. NULL when the provisioner JWK
    # (FILEARR_CA_PROVISIONER_JWK) is unset/malformed — the agent registers fine
    # but cannot fetch a cert until the operator plumbs the key (re-issue via
    # POST /agents/{id}/ca-ott once configured).
    ca_ott: str | None = None
    # W6-D2: null on success; set to a human-readable string when a supplied
    # ``config_group`` name did not resolve (agent enrolled with NULL group).
    config_group_warning: str | None = None


class CertBindIn(BaseModel):
    enroll_secret: str = Field(min_length=1)
    cert_fingerprint: str = Field(min_length=1, max_length=256)


class CaOttOut(BaseModel):
    """Response of the operator re-issue endpoint: a fresh step-ca OTT plus the
    same CA bootstrap the agent pins. Never carries a secret beyond the OTT
    (a short-lived, single-use bearer)."""

    ca_ott: str
    ca: CaBootstrap


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    platform: str
    rollout_group: str
    status: str
    cert_fingerprint: str | None
    last_contiguous_seq_no: int
    last_seen_at: datetime | None
    agent_version: str | None
    policy_version_applied: int | None
    revoked_at: datetime | None
    created_at: datetime
    # W6-D4: current config-group assignment (NULL = built-in defaults). Lets the
    # fleet console reflect + drive the inline group dropdown without an N+1.
    config_group_id: uuid.UUID | None
    # W6-D3: capability advertisement persisted from the agent's command poll
    # ({inventory_collectors, inventory_version}; NULL until the agent's first
    # post-W6 poll). The console offers only collectors an agent supports.
    capabilities: dict | None = None


def _ca_bootstrap(settings) -> CaBootstrap:
    """The public CA pinning/bootstrap material handed to an agent (never a
    secret — the root fingerprint is a pin, not a credential)."""
    return CaBootstrap(
        url=settings.ca_url,
        fingerprint=settings.ca_fingerprint,
        provisioner=settings.ca_provisioner,
        cert_ttl_hours=settings.agent_cert_ttl_hours,
    )


def _token_out(row: EnrollmentToken, now: datetime) -> TokenOut:
    if row.consumed_at is not None:
        st = "consumed"
    elif row.expires_at <= now:
        st = "expired"
    else:
        st = "active"
    return TokenOut(
        token_hash=row.token_hash,
        rollout_group=row.rollout_group,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        consumed_by=row.consumed_by,
        created_at=row.created_at,
        status=st,
    )


def _agent_out(a: Agent) -> AgentOut:
    return AgentOut(
        id=a.id,
        name=a.name,
        hostname=a.hostname,
        platform=a.platform,
        rollout_group=a.rollout_group,
        status=agentsync.agent_status(a),
        cert_fingerprint=a.cert_fingerprint,
        last_contiguous_seq_no=a.last_contiguous_seq_no,
        last_seen_at=a.last_seen_at,
        agent_version=a.agent_version,
        policy_version_applied=a.policy_version_applied,
        revoked_at=a.revoked_at,
        created_at=a.created_at,
        config_group_id=a.config_group_id,
        capabilities=a.capabilities,
    )


# --------------------------------------------------------------------------- #
# Enrollment tokens (admin)                                                    #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/enrollment-tokens",
    response_model=TokenMintOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def mint_token(
    body: TokenMintIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenMintOut:
    settings = get_settings()
    ttl_minutes = body.ttl_minutes or settings.enrollment_token_ttl_minutes
    raw, row = await agentsync.mint_enrollment_token(
        session,
        rollout_group=body.rollout_group,
        ttl_seconds=ttl_minutes * 60,
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_TOKEN_MINTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "token_hash": row.token_hash,  # scrubbed key name is fine; not the raw
            "rollout_group": row.rollout_group,
            "ttl_minutes": ttl_minutes,
        },
    )
    return TokenMintOut(
        token=raw,
        token_hash=row.token_hash,
        rollout_group=row.rollout_group,
        expires_at=row.expires_at,
    )


@router.get(
    "/agents/enrollment-tokens",
    response_model=list[TokenOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_tokens(
    session: AsyncSession = Depends(get_session),
) -> list[TokenOut]:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(EnrollmentToken).order_by(EnrollmentToken.created_at.desc())
        )
    ).scalars().all()
    return [_token_out(r, now) for r in rows]


@router.delete(
    "/agents/enrollment-tokens/{token_hash}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def revoke_token(
    token_hash: str,
    request: Request,
    force: bool = False,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an enrollment token row. An UNconsumed token deletes freely (it is
    a live credential being retired). A CONSUMED token is spent and harmless, but
    its row carries the ``consumed_by`` audit link — deleting it needs
    ``?force=true``, and the audit event captures that link before the row goes
    (so the trail survives the cleanup)."""
    row = await session.get(EnrollmentToken, token_hash)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such token")
    if row.consumed_at is not None and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "token already consumed; delete with ?force=true to clean it up",
        )
    consumed_by = str(row.consumed_by) if row.consumed_by else None
    await session.execute(
        delete(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash)
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_TOKEN_REVOKED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"token_hash": token_hash, "forced": force, "consumed_by": consumed_by},
    )


# --------------------------------------------------------------------------- #
# Register-first handshake (agent plane — token/secret authed, NOT API key)    #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/register",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled)],
)
async def register(
    body: RegisterIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RegisterOut:
    """R3: consume the enrollment token, assign the authoritative server-side
    ``agent_id``, and return it plus CA bootstrap info. The agent is created
    PENDING (no cert yet); it then CSRs against step-ca with the returned id in
    its CN/SAN and binds the fingerprint via ``/certificate``."""
    try:
        agent, raw_secret, config_group_warning = await agentsync.register_agent(
            session,
            raw_token=body.token,
            hostname=body.hostname,
            platform=body.platform,
            name=body.name,
            agent_version=body.agent_version,
            config_group=body.config_group,
        )
    except EnrollmentError as err:
        await session.rollback()
        # Audit the failed attempt (no secrets; the raw token never lands here).
        await audit.emit(
            audit.AGENT_REGISTERED,
            request=request,
            details={"outcome": "rejected", "reason": err.reason},
        )
        raise _enrollment_http(err) from err
    await session.commit()
    settings = get_settings()
    await audit.emit(
        audit.AGENT_REGISTERED,
        request=request,
        details={
            "outcome": "ok",
            "agent_id": str(agent.id),
            "hostname": agent.hostname,
            "platform": agent.platform,
            "rollout_group": agent.rollout_group,
        },
    )
    # Mint the scoped step-ca OTT (fail-safe: null when the provisioner JWK is
    # unset/malformed — registration already succeeded). Audit the mint by jti
    # only; the token itself never touches the log.
    ca_ott, jti = agentsync.try_mint_ca_ott(agent.id, settings)
    if ca_ott is not None:
        await audit.emit(
            audit.AGENT_CA_OTT_MINTED,
            request=request,
            details={"agent_id": str(agent.id), "jti": jti, "via": "register"},
        )
    return RegisterOut(
        agent_id=agent.id,
        rollout_group=agent.rollout_group,
        status="pending",
        enroll_secret=raw_secret,
        ca=_ca_bootstrap(settings),
        ca_ott=ca_ott,
        config_group_warning=config_group_warning,
    )


@router.post(
    "/agents/{agent_id}/certificate",
    response_model=AgentOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def bind_certificate(
    agent_id: uuid.UUID,
    body: CertBindIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    """Bind the CA-issued cert fingerprint (pending→active). Gated by the
    one-time enroll secret from register (P5-T2 hardens this behind mTLS)."""
    try:
        agent = await agentsync.bind_agent_certificate(
            session,
            agent_id=agent_id,
            raw_secret=body.enroll_secret,
            cert_fingerprint=body.cert_fingerprint,
        )
    except EnrollmentError as err:
        await session.rollback()
        raise _enrollment_http(err) from err
    await session.commit()
    await audit.emit(
        audit.AGENT_CERT_BOUND,
        request=request,
        details={"agent_id": str(agent_id), "cert_fingerprint": body.cert_fingerprint},
    )
    return _agent_out(agent)


# --------------------------------------------------------------------------- #
# CA one-time token re-issue (admin) — re-enrollment recovery path             #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/ca-ott",
    response_model=CaOttOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def reissue_ca_ott(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CaOttOut:
    """Operator-driven re-issue of a scoped step-ca OTT for an existing agent
    (spike's re-enrollment gap: a long-offline agent past its cert TTL, or one
    that registered before the provisioner JWK was configured). A PENDING or
    ACTIVE agent gets a fresh OTT; a REVOKED agent is refused (409). Audited by
    jti (never the token). 503 when the provisioner JWK is not configured — the
    endpoint's sole purpose is to mint, so there is no fail-safe null here."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent is revoked")
    settings = get_settings()
    ca_ott, jti = agentsync.try_mint_ca_ott(agent.id, settings)
    if ca_ott is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CA provisioner JWK not configured (cannot mint OTT)",
        )
    await audit.emit(
        audit.AGENT_CA_OTT_MINTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"agent_id": str(agent_id), "jti": jti, "via": "reissue"},
    )
    return CaOttOut(ca_ott=ca_ott, ca=_ca_bootstrap(settings))


# --------------------------------------------------------------------------- #
# Fleet console (admin)                                                        #
# --------------------------------------------------------------------------- #
class AgentFleetSummary(BaseModel):
    """W6-D4 status header tallies. ``connected``/``disconnected`` split the
    *active* (cert-bound, not revoked) agents by liveness against
    ``agent_online_threshold_seconds``; ``pending``/``revoked`` come straight from
    lifecycle status. ``total`` == connected + disconnected + pending + revoked."""

    total: int
    connected: int
    disconnected: int
    pending: int
    revoked: int


@router.get(
    "/agents/summary",
    response_model=AgentFleetSummary,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def agent_fleet_summary(
    session: AsyncSession = Depends(get_session),
) -> AgentFleetSummary:
    """Fleet status counts for the W6-D4 header, in ONE conditional-aggregation
    query (Postgres ``count(*) FILTER (WHERE …)`` — no N+1). Lifecycle mirrors
    :func:`agentsync.agent_status`: revoked (``revoked_at`` set) > active
    (``cert_fingerprint`` bound) > pending. An *active* agent is CONNECTED when it
    was last seen within ``agent_online_threshold_seconds``; otherwise (older, or
    never seen) DISCONNECTED."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.agent_online_threshold_seconds
    )
    # Lifecycle predicates.
    is_revoked = Agent.revoked_at.is_not(None)
    is_pending = Agent.revoked_at.is_(None) & Agent.cert_fingerprint.is_(None)
    is_active = Agent.revoked_at.is_(None) & Agent.cert_fingerprint.is_not(None)
    is_fresh = Agent.last_seen_at.is_not(None) & (Agent.last_seen_at >= cutoff)
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(is_revoked).label("revoked"),
                func.count().filter(is_pending).label("pending"),
                func.count().filter(is_active & is_fresh).label("connected"),
                func.count()
                .filter(
                    is_active
                    & or_(Agent.last_seen_at.is_(None), Agent.last_seen_at < cutoff)
                )
                .label("disconnected"),
            )
        )
    ).one()
    return AgentFleetSummary(
        total=row.total,
        connected=row.connected,
        disconnected=row.disconnected,
        pending=row.pending,
        revoked=row.revoked,
    )


@router.get(
    "/agents",
    response_model=list[AgentOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_agents(
    session: AsyncSession = Depends(get_session),
) -> list[AgentOut]:
    rows = (
        await session.execute(select(Agent).order_by(Agent.created_at.desc()))
    ).scalars().all()
    return [_agent_out(a) for a in rows]


@router.delete(
    "/agents/{agent_id}",
    response_model=AgentOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def revoke_agent(
    agent_id: uuid.UUID,
    request: Request,
    purge: bool = False,
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    """Application-layer kill switch (research §1.4): stamp ``revoked_at`` so the
    agent is denylisted on every replication/config request regardless of whether
    its short-lived cert is still cryptographically valid. NOT a hard delete —
    the row (and its future replication history) is retained. Idempotent.

    ``?purge=true`` HARD-deletes the row instead — the cleanup path for failed
    enrollments (pending rows) and decommissioned machines with no data
    footprint. Refused (409) while any library or item still references the
    agent: replicated data must keep its provenance, so a data-owning agent can
    only be revoked (or its libraries removed first). Cascades wipe the agent's
    commands/transfers/ledger/reconcile rows; ``libraries.source_agent_id`` and
    ``enrollment_tokens.consumed_by`` are SET NULL by their FKs."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if purge:
        lib_count = (
            await session.execute(
                select(func.count()).select_from(Library).where(
                    Library.source_agent_id == agent_id
                )
            )
        ).scalar_one()
        # items.source_agent_id carries no FK (provenance column) — guard in code
        # so a purge can never leave dangling ownership references.
        item_count = (
            await session.execute(
                select(func.count()).select_from(Item).where(
                    Item.source_agent_id == agent_id
                )
            )
        ).scalar_one()
        if lib_count or item_count:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"agent owns replicated data ({lib_count} library(ies), "
                f"{item_count} item(s)) — revoke it instead, or delete its "
                "libraries first",
            )
        snapshot = _agent_out(agent)
        await session.delete(agent)
        await session.commit()
        await audit.emit(
            audit.AGENT_DELETED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "agent_id": str(agent_id),
                "hostname": agent.hostname,
                "status": snapshot.status,
            },
        )
        return snapshot
    if agent.revoked_at is None:
        agent.revoked_at = datetime.now(UTC)
        await session.commit()
        await audit.emit(
            audit.AGENT_REVOKED,
            request=request,
            principal_id=audit.actor_id(request),
            details={"agent_id": str(agent_id), "hostname": agent.hostname},
        )
    return _agent_out(agent)
