"""add agent_share_maps (P10-T12 central share-mapping fallback)

Revision ID: d4e6f8a0c2b4
Revises: c1d2e3f4a5b6
Create Date: 2026-07-16 13:00:00.000000

User-mandated central fallback (docs/tasks/phase-10-agent-transfer-tasks.md,
P10-T12): when an agent cannot self-report a network ``ShareHint`` (P10-T11 is
best-effort, R1), an operator declares centrally how paths on an agent map to a
network share, so an agent-hosted item still renders a network-open link. This
is the per-AGENT equivalent of OPS-T7's deploy-time ``share-map.json`` (which
covers the CENTRAL server's own mounts / a library's ``share_prefix``).

Resolution reuses the vetted pure longest-``local_prefix``-wins resolver
(``transfers.resolve_share_url`` / ``share_map.resolve_for_agent``, R4) — the
same segment-boundary, case-preserving, separator-safe discipline as
``resolve_scan_path`` / the frontend ``pathlinks``. An agent-scoped mapping
outranks a global (``agent_id IS NULL`` = any agent) one of equal prefix length.

DDL deviations from the tasks-doc DDL (documented, additive — the doc DDL scoped
only id/agent_id/library_id/local_prefix/share_prefix/created_at, while this task
builds the full admin CRUD surface + UI-T15 both-format rendering the brief
mandates):
  * ``unc`` — optional Windows ``\\host\\share`` counterpart of ``share_prefix``
    (UI-T15 ShareLocation). When absent it is DERIVED from ``share_prefix`` at
    read time (SMB only); carrying it lets a deploy/operator pin the exact UNC.
  * ``storage_type`` / ``host`` — informational (mirrors ``share_map.py``'s
    ``ShareMapEntry`` and the deploy ``share-map.json`` shape), never used by the
    pure resolver.
  * ``updated_at`` — last-edit timestamp for the admin UI (edits are supported).
  * ``UNIQUE NULLS NOT DISTINCT (agent_id, library_id, local_prefix)`` — the
    doc's "unique per (agent_id, prefix)" hardened to also key on the optional
    ``library_id`` scope and to treat a NULL ``agent_id``/``library_id`` as a
    single value (PG15+; the DB back-stops the app-level 409 dup check). Renamed
    ``agents`` FK is ``ON DELETE CASCADE`` (a revoked/removed agent's mappings go
    with it); ``library_id`` FK likewise.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e6f8a0c2b4"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_share_maps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        # NULL = any agent (global fallback); a concrete id scopes to one agent.
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Optional additional scope to one library.
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("local_prefix", sa.Text(), nullable=False),
        sa.Column("share_prefix", sa.Text(), nullable=False),
        sa.Column("unc", sa.Text(), nullable=True),
        sa.Column("storage_type", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="CASCADE",
            name="fk_agent_share_maps_agent_id_agents",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["libraries.id"], ondelete="CASCADE",
            name="fk_agent_share_maps_library_id_libraries",
        ),
    )
    # Doc DDL index: fast per-agent lookup of an agent's mappings.
    op.create_index(
        "ix_agent_share_maps_agent", "agent_share_maps", ["agent_id"]
    )
    # Uniqueness backstop for the app-level 409 dup-prefix check. NULLS NOT
    # DISTINCT (PG15+) so two identical global (NULL agent) rules for the same
    # prefix cannot both exist.
    op.create_index(
        "uq_agent_share_maps_scope_prefix",
        "agent_share_maps",
        ["agent_id", "library_id", "local_prefix"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_agent_share_maps_scope_prefix", table_name="agent_share_maps"
    )
    op.drop_index("ix_agent_share_maps_agent", table_name="agent_share_maps")
    op.drop_table("agent_share_maps")
