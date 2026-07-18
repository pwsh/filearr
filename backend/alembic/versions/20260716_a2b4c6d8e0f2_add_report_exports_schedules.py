"""add report_exports + report_schedules (P11-T5/T9/T11)

Revision ID: a2b4c6d8e0f2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Completes Phase 11: the background-export job lifecycle (``report_exports``) and
scheduled-delivery definitions (``report_schedules``). Both carry a source XOR
(a custom ``report_definition_id`` OR a ``canned_report_key``); ``report_exports``
tracks the queued->running->complete/failed lifecycle + the produced artifact
(retained past its TTL only as an audit row, ``purged_at`` stamped), and
``report_schedules`` rides the ``scan_cron`` once-per-occurrence machinery
(``last_cron_fired_at``) to fire an export delivered through a Phase-8
``alert_channels`` row.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2b4c6d8e0f2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_schedules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=True),
        sa.Column(
            "report_definition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("canned_report_key", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "format", sa.Text(), nullable=False, server_default=sa.text("'csv'")
        ),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_cron_fired_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "num_nonnulls(report_definition_id, canned_report_key) = 1",
            name="report_schedule_source_xor",
        ),
        sa.ForeignKeyConstraint(
            ["report_definition_id"], ["report_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["alert_channels.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_report_schedules_owner", "report_schedules", ["owner_principal"]
    )
    op.create_index(
        "ix_report_schedules_enabled",
        "report_schedules",
        ["enabled"],
        postgresql_where=sa.text("enabled"),
    )

    op.create_table(
        "report_exports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "report_definition_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("canned_report_key", sa.Text(), nullable=True),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "triggered_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("owner_principal", sa.Text(), nullable=True),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("delivery_status", sa.Text(), nullable=True),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','complete','failed')",
            name="report_export_status_valid",
        ),
        sa.CheckConstraint(
            "num_nonnulls(report_definition_id, canned_report_key) = 1",
            name="report_export_source_xor",
        ),
        sa.ForeignKeyConstraint(
            ["report_definition_id"], ["report_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["report_schedules.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_report_exports_owner", "report_exports", ["owner_principal"])
    op.create_index("ix_report_exports_status", "report_exports", ["status"])
    op.create_index(
        "ix_report_exports_expires",
        "report_exports",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL AND purged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_report_exports_expires", table_name="report_exports")
    op.drop_index("ix_report_exports_status", table_name="report_exports")
    op.drop_index("ix_report_exports_owner", table_name="report_exports")
    op.drop_table("report_exports")
    op.drop_index("ix_report_schedules_enabled", table_name="report_schedules")
    op.drop_index("ix_report_schedules_owner", table_name="report_schedules")
    op.drop_table("report_schedules")
