"""P10-T10 — hosting-agent identity, online status, and verify freshness for an
agent-hosted item (item-detail UI).

``GET /items/{item_id}/agent-status`` surfaces, for an item whose library is owned
by a fleet agent (``libraries.source_agent_id``, the same ownership authority the
verify (P10-T3) and transfer (P10-T13) paths key on):

* which agent hosts it (``agent_id`` / ``agent_name``) and its lifecycle
  (``agent_status``: ``active`` / ``revoked`` / ``pending``);
* whether that agent is **online right now** — ``last_seen_at`` (refreshed on every
  command poll / replication batch) within ``FILEARR_AGENT_ONLINE_THRESHOLD_SECONDS``
  — plus the raw ``last_seen_at`` so the UI can render a relative "last seen";
* the ``last_verified_at`` freshness stamp (P10-T3) driving the "last verified …"
  line; and
* ``verify_in_flight`` — a pending/picked-up ``stat_check`` or ``rehash_check`` for
  this item already exists — so the UI disables the Verify button until it lands.

A **centrally-scanned** item (its library has no ``source_agent_id``) returns
``{"agent_hosted": false}`` (200): it has local bytes and no hosting agent, which is
the normal, non-error case — the caller simply renders no agent panel.

Read-tier: ``require_permission("search_metadata")`` + ``authorize_item`` (404 for an
item outside the caller's read scope, mirroring ``GET /items/{id}``). This is a pure
read — no side effects, no audit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, Item, Library
from filearr.security import PermissionContext, require_permission
from filearr.verify import VERIFY_KINDS

router = APIRouter()

#: Command lifecycle states that count as an in-flight verify (not yet terminal).
_IN_FLIGHT = ("pending", "picked_up")


def _agent_status(agent: Agent) -> str:
    """Lifecycle label: ``revoked`` (kill-switched), ``pending`` (registered but no
    cert bound yet — cannot act), else ``active``."""
    if agent.revoked_at is not None:
        return "revoked"
    if not agent.cert_fingerprint:
        return "pending"
    return "active"


@router.get("/{item_id}/agent-status")
async def item_agent_status(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict:
    """Hosting-agent identity + online/verify state for an item (P10-T10).

    404 for an unknown item (or one outside the caller's read scope);
    ``{"agent_hosted": false}`` for a centrally-scanned item; otherwise the agent
    panel payload."""
    item = (
        await session.execute(select(Item).where(Item.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    ctx.authorize_item(item)  # 404 for an item outside the caller's read scope

    library = await session.get(Library, item.library_id)
    agent_id = library.source_agent_id if library is not None else None
    if agent_id is None:
        return {"agent_hosted": False}
    agent = await session.get(Agent, agent_id)
    if agent is None:  # dangling ownership ref — treat as not hosted (no fabrication)
        return {"agent_hosted": False}

    now = datetime.now(UTC)
    threshold = get_settings().agent_online_threshold_seconds
    online = (
        agent.last_seen_at is not None
        and (now - agent.last_seen_at).total_seconds() <= threshold
    )

    in_flight = (
        await session.execute(
            select(AgentCommand.id)
            .where(
                AgentCommand.item_id == item_id,
                AgentCommand.kind.in_(tuple(VERIFY_KINDS)),
                AgentCommand.status.in_(_IN_FLIGHT),
            )
            .limit(1)
        )
    ).first() is not None

    return {
        "agent_hosted": True,
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "agent_status": _agent_status(agent),
        "online": online,
        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        "last_verified_at": (
            item.last_verified_at.isoformat() if item.last_verified_at else None
        ),
        "verify_in_flight": in_flight,
    }
