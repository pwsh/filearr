"""P3-T7 saved-search API schemas (params-based).

A saved search is a named, persisted wrapper around the flat ``/search`` query
params (stored as JSONB in ``saved_searches.params``). Not a Meilisearch concept
— pure Postgres, trivially rebuild-compatible (never touches the index,
invariant 1). The router validates every ``params`` key against
``filearr.api.search.SEARCH_PARAM_NAMES`` (derived from the ``/search`` endpoint
signature) BEFORE persisting, so an unknown/renamed key is a 422 rather than a
silently-stored dead value.

R7: ``owner_principal`` is a nullable placeholder from day one — enforcement is
deferred to phase 6 (identity/auth/RBAC), but the field exists now to avoid an
awkward later migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SavedSearchIn(BaseModel):
    """Create payload: a named bundle of ``/search`` params (stored verbatim)."""

    name: str = Field(min_length=1, max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    # R7 placeholder; phase-6 RBAC binds/enforces this. Nullable until then.
    owner_principal: str | None = None


class SavedSearchUpdate(BaseModel):
    """Partial edit (rename and/or replace params). ``model_fields_set`` drives
    which columns are touched, so a PATCH with only ``name`` never clears
    ``params`` (and vice-versa)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    params: dict[str, Any] | None = None


class SavedSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    params: dict[str, Any]
    owner_principal: str | None = None
    created_at: datetime
    updated_at: datetime
