"""P3-T7 — named, persisted ``/search`` queries (brief §7 P1).

A saved search is a stored bundle of the flat ``/search`` params (JSONB
``params``), replayed by re-running the endpoint — pure Postgres, never a
Meilisearch concept (invariant 1). CRUD scopes: GET is read; POST/PATCH/DELETE
are write. ``owner_principal`` is populated best-effort now (R7); phase-6 RBAC
will enforce per-owner ACLs (the ``UNIQUE(owner_principal, name)`` constraint
already scopes name uniqueness per owner).

Param validation (integrity): every key of ``params`` is checked against
``filearr.api.search.SEARCH_PARAM_NAMES`` — the frozenset DERIVED from the
``/search`` endpoint signature — on BOTH create and update. An unknown/renamed
key is a 422 (never silently stored), so a saved query can only ever hold keys
the search endpoint actually accepts. A duplicate ``(owner_principal, name)`` is
a 409.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.api.search import SEARCH_PARAM_NAMES
from filearr.db import get_session
from filearr.models import SavedSearch
from filearr.schemas_search import SavedSearchIn, SavedSearchOut, SavedSearchUpdate
from filearr.security import require_scope

router = APIRouter()


def _validate_params(params: dict) -> None:
    """Reject any ``params`` key the ``/search`` endpoint does not accept.

    The check is the SAME frozenset the endpoint's signature yields, so a saved
    query can only carry live params — a renamed/removed search param surfaces
    here as a 422 (and, in the test suite, as a round-trip failure)."""
    unknown = sorted(set(params) - SEARCH_PARAM_NAMES)
    if unknown:
        raise HTTPException(
            422,
            f"unknown search param(s) {unknown}; allowed: {sorted(SEARCH_PARAM_NAMES)}",
        )


@router.get(
    "", response_model=list[SavedSearchOut], dependencies=[Depends(require_scope("read"))]
)
async def list_saved_searches(
    owner_principal: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[SavedSearch]:
    """All saved searches (read scope), name-ordered. Optional ``owner_principal``
    narrows to one principal's saves (R7 — no enforcement yet, just a filter)."""
    stmt = select(SavedSearch).order_by(SavedSearch.name)
    if owner_principal is not None:
        stmt = stmt.where(SavedSearch.owner_principal == owner_principal)
    return list((await session.execute(stmt)).scalars().all())


@router.get(
    "/{search_id}",
    response_model=SavedSearchOut,
    dependencies=[Depends(require_scope("read"))],
)
async def get_saved_search(
    search_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> SavedSearch:
    row = (
        await session.execute(select(SavedSearch).where(SavedSearch.id == search_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "saved search not found")
    return row


@router.post(
    "",
    response_model=SavedSearchOut,
    status_code=201,
    dependencies=[Depends(require_scope("write"))],
)
async def create_saved_search(
    body: SavedSearchIn, session: AsyncSession = Depends(get_session)
) -> SavedSearch:
    """Create a saved search. Validates every ``params`` key against the search
    vocabulary (422 on unknown) BEFORE persisting; a duplicate (owner, name) is
    a 409."""
    _validate_params(body.params)
    row = SavedSearch(
        name=body.name,
        owner_principal=body.owner_principal,
        params=body.params,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, f"a saved search named {body.name!r} already exists for this owner"
        ) from exc
    await session.refresh(row)
    return row


@router.patch(
    "/{search_id}",
    response_model=SavedSearchOut,
    dependencies=[Depends(require_scope("write"))],
)
async def update_saved_search(
    search_id: uuid.UUID,
    body: SavedSearchUpdate,
    session: AsyncSession = Depends(get_session),
) -> SavedSearch:
    """Rename and/or replace params. Only the fields present in the PATCH body are
    touched; a replaced ``params`` is re-validated (422 on unknown key); a rename
    that collides with another save for the same owner is a 409."""
    row = (
        await session.execute(select(SavedSearch).where(SavedSearch.id == search_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "saved search not found")

    sent = body.model_fields_set
    if "params" in sent and body.params is not None:
        _validate_params(body.params)
        row.params = body.params
    if "name" in sent and body.name is not None:
        row.name = body.name
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, f"a saved search named {row.name!r} already exists for this owner"
        ) from exc
    await session.refresh(row)
    return row


@router.delete(
    "/{search_id}", status_code=204, dependencies=[Depends(require_scope("write"))]
)
async def delete_saved_search(
    search_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    row = (
        await session.execute(select(SavedSearch).where(SavedSearch.id == search_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "saved search not found")
    await session.execute(sa_delete(SavedSearch).where(SavedSearch.id == search_id))
    await session.commit()
