"""add agent_replication_log + agent-owned library columns (P5-T4)

Revision ID: e7f1a9c3d5b2
Revises: d4e6f8a0c2b4
Create Date: 2026-07-16 14:00:00.000000

Phase 5, P5-T4 (the central-side replication apply path). Two additive changes:

  * ``agent_replication_log`` — the per-agent idempotency ledger. One row per
    applied outbox entry, composite PK ``(agent_id, seq_no)``. ``apply_batch``
    inserts a row for EVERY entry in an accepted batch (``ON CONFLICT DO
    NOTHING``) in the SAME transaction that writes items + advances
    ``agents.last_contiguous_seq_no`` — a backstop against double-apply behind the
    endpoint's ``check_batch`` seq-continuation gate. ``item_id`` is nullable +
    ``ON DELETE SET NULL`` (an R2 no-op tombstone / a collapsed-away entry has no
    item; a later recycle-bin purge must not vacate the ledger's history).

  * ``libraries.source_agent_id`` / ``libraries.agent_library_ref`` — central
    AUTO-PROVISIONS a Library per (agent, library_ref) at apply time (ruling R1).
    ``source_agent_id`` FK ``agents.id ON DELETE SET NULL`` (removing an agent
    orphans its replicated catalog rather than cascade-deleting it);
    ``agent_library_ref`` is the agent-opaque ref the library materializes. A
    PARTIAL UNIQUE on (source_agent_id, agent_library_ref) WHERE source_agent_id
    IS NOT NULL makes a repeat batch reuse the same row while the many
    locally-scanned libraries (both columns NULL) never collide on it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7f1a9c3d5b2"
down_revision: str | None = "d4e6f8a0c2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- agent-owned library provenance columns (ruling R1) ------------------
    op.add_column(
        "libraries",
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "libraries",
        sa.Column("agent_library_ref", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_libraries_source_agent_id_agents",
        "libraries",
        "agents",
        ["source_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Partial unique: only among agent-owned libraries; central-scanned libraries
    # (source_agent_id NULL) are excluded so their NULL/NULL pairs never collide.
    op.create_index(
        "uq_libraries_source_agent_ref",
        "libraries",
        ["source_agent_id", "agent_library_ref"],
        unique=True,
        postgresql_where=sa.text("source_agent_id IS NOT NULL"),
    )

    # --- per-agent replication idempotency ledger ----------------------------
    op.create_table(
        "agent_replication_log",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq_no", sa.BigInteger(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Composite PK = the (agent_id, seq_no) idempotency key.
        sa.PrimaryKeyConstraint("agent_id", "seq_no"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: a purged item must not vacate the ledger row.
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_agent_replication_log_item", "agent_replication_log", ["item_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_replication_log_item", table_name="agent_replication_log")
    op.drop_table("agent_replication_log")
    op.drop_index("uq_libraries_source_agent_ref", table_name="libraries")
    op.drop_constraint(
        "fk_libraries_source_agent_id_agents", "libraries", type_="foreignkey"
    )
    op.drop_column("libraries", "agent_library_ref")
    op.drop_column("libraries", "source_agent_id")
