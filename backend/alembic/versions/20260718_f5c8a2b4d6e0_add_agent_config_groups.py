"""add agent_config_groups + agents.config_group_id (W6-D2)

Revision ID: f5c8a2b4d6e0
Revises: e4a7c2f1b9d6
Create Date: 2026-07-18 15:00:00.000000

Wave 6, W6-D2 (central-side agent configuration groups + remote configuration).

A **config group** is a named, reusable bundle of remote-configuration settings
(``log_level``, ``scan_selections`` path selections, ``inventory`` collectors,
``scan_schedule_cron``) an operator authors once and assigns to many agents. It
is a NEW grouping dimension, ORTHOGONAL to ``agents.rollout_group`` (the P5-T7
release-canary group — never reused here).

Two additive objects:

  * ``agent_config_groups`` — id (uuidv7 PK), ``name`` UNIQUE NOT NULL,
    ``description`` NULL, ``settings`` JSONB NOT NULL DEFAULT '{}', created/updated.
    ``settings`` is validated by the API (``filearr.agent_config.validate_settings``)
    before it ever lands here; the DB stores it verbatim.
  * ``agents.config_group_id`` — UUID NULL FK → ``agent_config_groups.id``
    ON DELETE SET NULL. **NULL is the default** (built-in agent defaults); a
    "default" group is NOT special-cased. Deleting a group with members lets the
    FK SET NULL them (members fall back to built-in defaults).

Purely additive; the downgrade drops the column then the table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f5c8a2b4d6e0"
down_revision: str | None = "e4a7c2f1b9d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_config_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.UniqueConstraint("name", name="uq_agent_config_groups_name"),
    )

    op.add_column(
        "agents",
        sa.Column("config_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_config_group",
        "agents",
        "agent_config_groups",
        ["config_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_agents_config_group_id", "agents", ["config_group_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_config_group_id", table_name="agents")
    op.drop_constraint("fk_agents_config_group", "agents", type_="foreignkey")
    op.drop_column("agents", "config_group_id")
    op.drop_table("agent_config_groups")
