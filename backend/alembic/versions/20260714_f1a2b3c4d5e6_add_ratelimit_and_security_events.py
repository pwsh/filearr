"""P6-T8/T9 security hardening: rate-limit state + security_events audit log

Revision ID: f1a2b3c4d5e6
Revises: b8f1d3c2a9e4
Create Date: 2026-07-14 09:00:00.000000

Two additive, zero-behaviour-change tables for the Wave-4 auth-hygiene close-out.
Nothing here changes an existing code path until auth is enabled and the
credential-check hooks fire; an auth-off deployment never writes a row.

1. ``auth_rate_limits`` (P6-T8) — Postgres-backed fixed-window brute-force
   counter + lock. One row per (bucket_kind, bucket_key) where bucket_kind is
   ``username`` (the submitted string — catches a distributed brute force) or
   ``ip`` (the source address). Chosen over slowapi/in-memory so the state
   survives a restart and is shared across workers, with no Redis dependency.

2. ``security_events`` (P6-T9) — append-only audit log for login/logout,
   account lifecycle, grant/group changes, lockouts and (opt-in) reads.
   ``principal_id`` SET NULLs on principal delete so history outlives the
   account. A keyset (ts DESC, id DESC) index backs the admin audit feed.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "b8f1d3c2a9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limits",
        sa.Column("bucket_kind", sa.Text(), nullable=False),
        sa.Column("bucket_key", sa.Text(), nullable=False),
        sa.Column(
            "window_start",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("bucket_kind", "bucket_key"),
        sa.CheckConstraint(
            "bucket_kind IN ('username','ip')", name="auth_rate_limits_kind_valid"
        ),
    )
    op.create_index(
        "ix_auth_rate_limits_locked", "auth_rate_limits", ["locked_until"]
    )

    op.create_table(
        "security_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("username_attempted", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="SET NULL"
        ),
    )
    # Keyset pagination index for the admin audit feed (newest first).
    op.execute(
        "CREATE INDEX ix_security_events_ts_id ON security_events (ts DESC, id DESC)"
    )
    op.create_index(
        "ix_security_events_principal", "security_events", ["principal_id"]
    )
    op.create_index("ix_security_events_type", "security_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_security_events_type", table_name="security_events")
    op.drop_index("ix_security_events_principal", table_name="security_events")
    op.drop_index("ix_security_events_ts_id", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_auth_rate_limits_locked", table_name="auth_rate_limits")
    op.drop_table("auth_rate_limits")
