"""add agent reconcile sweep tables + purge-safety watermark (P5-T5)

Revision ID: f8a1c3e5b7d9
Revises: e7f1a9c3d5b2
Create Date: 2026-07-17 10:00:00.000000

Phase 5, P5-T5 (central-side full-manifest reconciliation sweep). Three additive
changes:

  * ``agents.last_reconcile_at`` — the §4.5 purge-safety watermark. Stamped when
    the agent's last full reconciliation completed (start-match OR finish);
    ``worker.purge_recycle_bin`` skips a trashed agent item whose ``deleted_at``
    the last sweep has not yet observed (NULL or older watermark) while the owning
    agent is live (non-revoked). Gates permanent purge alongside the retention
    window (R2). NULL = never reconciled.

  * ``agent_reconcile_sessions`` — one in-flight sweep per agent. ``unique(agent_id)``
    enforces exactly one live session (a new start supersedes any prior unfinished
    one). TTL expiry is by ``started_at`` (no column — compared to now-TTL).

  * ``agent_reconcile_staging`` — the agent's paged full manifest. PK
    ``(session_id, rel_path)`` makes a re-sent page idempotent; ``mtime_us`` is
    INTEGER microseconds (the cross-language digest quantum, ruling 2). CASCADE on
    the session FK so dropping/expiring a session reclaims its staging.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f8a1c3e5b7d9"
down_revision: str | None = "e7f1a9c3d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- purge-safety watermark (§4.5) ---------------------------------------
    op.add_column(
        "agents",
        sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- one live reconcile session per agent --------------------------------
    op.create_table(
        "agent_reconcile_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_ref", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "staged_rows", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
    )
    # Exactly one live session per agent (a new start supersedes the prior one).
    op.create_index(
        "uq_agent_reconcile_sessions_agent",
        "agent_reconcile_sessions",
        ["agent_id"],
        unique=True,
    )

    # --- staged manifest rows (µs mtime; upsert per re-sent page) -------------
    op.create_table(
        "agent_reconcile_staging",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_us", sa.BigInteger(), nullable=False),
        sa.Column("quick_hash", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id", "rel_path"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_reconcile_sessions.id"],
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_reconcile_staging")
    op.drop_index(
        "uq_agent_reconcile_sessions_agent", table_name="agent_reconcile_sessions"
    )
    op.drop_table("agent_reconcile_sessions")
    op.drop_column("agents", "last_reconcile_at")
