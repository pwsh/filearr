"""P11-T8 — custom-report definition API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_FORMATS = ("csv", "json", "ndjson", "xml")


class ReportDefinitionIn(BaseModel):
    """Create payload for a custom report."""

    name: str = Field(min_length=1, max_length=200)
    query: str = Field(default="", max_length=4000)
    columns: list[str] = Field(default_factory=list)
    sort: str | None = None
    format: str = Field(default="csv", pattern="^(csv|json|ndjson|xml)$")
    owner_principal: str | None = None


class ReportDefinitionUpdate(BaseModel):
    """Partial edit; ``model_fields_set`` drives which columns are written."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    query: str | None = Field(default=None, max_length=4000)
    columns: list[str] | None = None
    sort: str | None = None
    format: str | None = Field(default=None, pattern="^(csv|json|ndjson|xml)$")


class ReportDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_principal: str | None
    query: str
    columns: list[str]
    sort: str | None
    format: str
    created_at: datetime
    updated_at: datetime


class ReportValidateIn(BaseModel):
    """Dry-run validation payload for the live UI validator."""

    query: str = Field(default="", max_length=4000)
    columns: list[str] = Field(default_factory=list)
    sort: str | None = None


class QueryPreviewIn(BaseModel):
    """Live-preview payload for the visual filter builder (``POST /query/preview``).

    ``query`` is a querydsl string (the builder compiles rows -> DSL as the single
    source of truth); ``limit`` is a small page size (a preview is a spot-check),
    capped server-side. Reuses the same 4000-char query ceiling as report defs."""

    query: str = Field(default="", max_length=4000)
    limit: int = Field(default=25, ge=1, le=50)
    offset: int = Field(default=0, ge=0)
