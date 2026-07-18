"""add policy_versions (agent config/policy push) — P5-T6

Revision ID: a3f7c1e9b5d4
Revises: f8a1c3e5b7d9
Create Date: 2026-07-17 14:00:00.000000

Phase 5, P5-T6 (central → agent config/policy push, research §6.3). One additive
table, the audit-trailed source of policy an agent polls at
``GET /agents/{id}/policy``:

  * ``policy_versions`` — append-only versioned policy rows. One row per
    (scope, version); an edit inserts a NEW row at ``version = prior scope max +
    1`` (old rows are never mutated — the history is the audit trail). ``scope_type``
    is CHECK-constrained to ``global`` | ``group`` | ``agent``; ``scope_id`` is
    NULL for global, the ``rollout_group`` name for group, the agent UUID-as-text
    for agent. ``policy`` is JSONB stored verbatim (unknown forward-compat keys
    preserved). ``UNIQUE (scope_type, scope_id, version)`` + a descending index
    ``(scope_type, scope_id, version DESC)`` for the "current row per scope" and
    "history desc" reads.

This is the §6.3 ``policy_versions`` table; **a future phase-2 Stage B policy-
versioning feature REUSES this exact table** (P5-T6 shipped before P2 Stage B),
so no second policy-versioning schema should be introduced.

Note: because ``scope_id`` is NULL for the global scope and Postgres treats NULLs
as distinct in a UNIQUE constraint, global-scope version uniqueness is enforced by
the application's ``max(version)+1`` append (single-operator write path); the
constraint is a hard backstop for the group/agent scopes (non-null ``scope_id``).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f7c1e9b5d4"
down_revision: str | None = "f8a1c3e5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "scope_type IN ('global','group','agent')",
            name="policy_versions_scope_type_valid",
        ),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "version",
            name="uq_policy_versions_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_policy_versions_scope_version",
        "policy_versions",
        ["scope_type", "scope_id", sa.text("version DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_policy_versions_scope_version", table_name="policy_versions")
    op.drop_table("policy_versions")
