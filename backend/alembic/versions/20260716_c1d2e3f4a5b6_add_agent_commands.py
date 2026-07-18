"""add agent_commands (P10-T1 on-demand agent command primitive)

Revision ID: c1d2e3f4a5b6
Revises: b4d8f0a2c6e4
Create Date: 2026-07-16 12:00:00.000000

Phase 10 wave opener: the on-demand instruction channel distinct from Phase-5's
policy/replication channels (research §3.1, osquery ``distributed_interval``
precedent). Central enqueues one command per row; an agent polls, picks it up,
and reports a ``result``; a periodic TTL sweep expires stale rows and re-queues
unacked deliveries. See docs/tasks/phase-10-agent-transfer-tasks.md (P10-T1).

DDL deviations from the tasks-doc DDL (documented, additive — the doc's P10-T1
scoped only the table + poll + sweep, while this task builds the full central-
side admin/API surface the orchestrator brief mandates):
  * ``status`` CHECK adds ``'cancelled'`` — the admin cancel action is a distinct
    terminal state (an operator abandoning a pre-terminal command), not the same
    as ``expired`` (TTL lapsed) or ``failed`` (agent tried + failed). The UI chip
    and the security_events audit both need to tell them apart.
  * ``attempts INTEGER`` — bounds redelivery of unacked deliveries independent of
    the wall-clock ``expires_at`` bound (a persistently-crashing agent stops
    being re-queued after FILEARR_AGENT_COMMAND_MAX_ATTEMPTS).
  * ``updated_at TIMESTAMPTZ`` — last-transition timestamp (UI + keyset stability).
  * ``requested_by`` uses ``ON DELETE SET NULL`` (not a bare FK) so the audit
    actor link outlives a deleted principal, mirroring ``security_events``.
The doc's own columns/index (``ix_agent_commands_pending``) are preserved
verbatim; the two extra indexes serve the admin keyset list and the sweep.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b4d8f0a2c6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_commands",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('stat_check','rehash_check','stage_upload')",
            name="agent_commands_kind_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','picked_up','done','failed','expired','cancelled')",
            name="agent_commands_status_valid",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["principals.id"], ondelete="SET NULL"
        ),
    )
    # Doc DDL index: fast per-agent FIFO drain of undelivered work.
    op.create_index(
        "ix_agent_commands_pending",
        "agent_commands",
        ["agent_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Keyset list ordering (id is uuidv7 → time-ordered), admin console.
    op.create_index(
        "ix_agent_commands_id_desc", "agent_commands", [sa.text("id DESC")]
    )
    # Sweep target: only ever scans non-terminal rows (expiry + redelivery).
    op.create_index(
        "ix_agent_commands_sweep",
        "agent_commands",
        ["expires_at"],
        postgresql_where=sa.text("status IN ('pending','picked_up')"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_commands_sweep", table_name="agent_commands")
    op.drop_index("ix_agent_commands_id_desc", table_name="agent_commands")
    op.drop_index("ix_agent_commands_pending", table_name="agent_commands")
    op.drop_table("agent_commands")
