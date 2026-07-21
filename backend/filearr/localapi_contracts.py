"""Agent local-API wire contract — inert pydantic mirror of the Go structs
(Phase 7, roadmap §2 / ``docs/research/phase-7-local-query-access.md`` §3).

**Inert scaffolding.** Nothing in the runtime imports this module — only the
Phase-7 scaffolding tests do. The offline agent's local query surface
(``agent/internal/localapi``, see ``agent/docs/layout.md``) is written in **Go**;
these pydantic models exist so the Python side has a checked-in, reviewable,
version-controlled statement of the request/response JSON the CLI and local web
UI exchange with the agent daemon over its Unix-socket / named-pipe transport
(brief §3.1/§3.2). They are documentation-as-code, not a runtime dependency.

**JSON casing rule (contract):** every field name here is the exact JSON key on
the wire, in ``snake_case``, matching the rest of the Filearr API surface
(``rel_path``, ``content_hash``, …). The Go structs carry explicit
``json:"snake_case"`` tags to match; the Go field identifiers are UpperCamel but
their tags are authoritative. Unknown keys are rejected (``extra="forbid"``) so a
drift between the Go emitter and this mirror is caught in review, not silently
tolerated.

Rulings reflected here:

- **R1** — result rows carry only the local-index (narrow) field set:
  path / size / mtime / hashes / filename-derived title. No central-only
  extracted metadata crosses this boundary.
- **R3** — :class:`ScopeInfo` is REQUIRED on every :class:`QueryResponse`; its
  ``active`` flag drives the "restricted view" affordance the CLI/web UI must
  surface. Silent scope filtering is forbidden.
- **read-only** — :class:`HealthResponse.read_only` is always ``True``; the
  surface never exposes a write path (brief §3.4).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Wire(BaseModel):
    """Base: forbid unknown keys so Go↔Python drift surfaces in review."""

    model_config = ConfigDict(extra="forbid")


class QueryRequest(_Wire):
    """CLI/UI → agent. ``query`` is a raw DSL string (see :mod:`filearr.querydsl`);
    the agent parses it locally — the client never sends a pre-parsed AST, and
    never sends a scope predicate (scope is server-cached and applied by the
    agent, never trusted from the client; brief §4.4)."""

    query: str
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ResultRow(_Wire):
    """One matched item. R1 narrow field set only."""

    id: str
    rel_path: str
    filename: str
    extension: str | None = None
    size: int
    mtime: str  # ISO-8601 UTC
    kind: str | None = None  # taxonomy file_category classification, if known
    quick_hash: str | None = None
    content_hash: str | None = None
    fuzzy_matched: bool = False  # matched via the edit-distance re-rank, not exact
    score: float | None = None


class ScopeInfo(_Wire):
    """R3 restricted-view affordance. ``active`` is true whenever a path-scope
    predicate narrowed the result set; ``predicates`` are the flattened
    prefix/glob predicates in force (central flattens RBAC grants at push time,
    ruling R2 — the agent stores and applies, never evaluates rules)."""

    active: bool = False
    predicates: list[str] = Field(default_factory=list)
    stale: bool = False  # cached policy is past its refresh window (most-restrictive applied)


class QueryResponse(_Wire):
    """agent → CLI/UI."""

    rows: list[ResultRow]
    total: int  # rows matched before limit/offset
    truncated: bool  # total > returned window
    fuzzy: bool  # the fuzzy re-rank layer was engaged for this query
    scope: ScopeInfo  # REQUIRED (R3) — always present, even when inactive
    elapsed_ms: int
    notice: str | None = None  # e.g. typo-tolerance-gap copy (brief §4.3)


class HealthResponse(_Wire):
    """agent → CLI/UI health/status probe."""

    status: str  # "ok" | "degraded" | "starting"
    index_ready: bool
    item_count: int
    read_only: bool = True  # invariant — never a write surface (brief §3.4)
    web_ui_enabled: bool = False
    auth_required: bool = False
    policy_version: int | None = None
    policy_stale: bool = False  # last policy fetch is past the offline grace window
    offline_grace_expires_at: str | None = None  # ISO-8601, when the cache goes stale
