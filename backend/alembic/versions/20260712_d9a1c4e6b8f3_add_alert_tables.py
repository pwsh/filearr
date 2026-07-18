"""add alerting tables: alert_channels/alert_rules/alert_rule_channels/alert_events (P8-T1)

Revision ID: d9a1c4e6b8f3
Revises: c1e3a5b7d9f2
Create Date: 2026-07-12 12:00:00.000000

Phase 8 alerting schema (brief §8.1, tasks doc § Intended schema). Four new
tables, fully additive — a database that never configures an alert rule keeps
every table empty and pays only their (empty) presence:

  * ``alert_channels`` — notification destinations. ``config`` JSONB holds
    per-channel settings; its *secret* sub-fields are AES-GCM ciphertext strings
    (P8-T4), never plaintext. ``dispatch_locality`` (R6) is an authoritative
    admin choice (``central``/``agent``). ``type`` CHECK-constrained to the three
    supported kinds.
  * ``alert_rules`` — file-watch + ``is_system`` operational rules (one shape,
    dogfooded). ``group_by`` fixed to ``{event_type,library_id,rule_id}`` (R1).
    ``library_id`` FK CASCADE (null = all libraries).
  * ``alert_rule_channels`` — many-to-many fan-out (rule -> channels).
  * ``alert_events`` — match record + delivery queue + digest buffer (§3.3):
    written only on an actual rule match (no unconditional per-file log). The
    partial ``ix_alert_events_pending`` index serves group-wait/digest buffering
    (undelivered rows per dedup_key); ``ix_alert_events_rule_delivered_at`` serves
    the P8-T15 rolling-hour dispatch ceiling and repeat-interval bookkeeping.

Downgrade drops all four in FK-safe order (events -> junction -> rules ->
channels). Nothing outside this migration references these tables, so the
downgrade is clean.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9a1c4e6b8f3"
down_revision: str | None = "c1e3a5b7d9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_channels",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "dispatch_locality",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'central'"),
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_alert_channels_name"),
        sa.CheckConstraint(
            "type IN ('webhook','email','apprise')", name="alert_channel_type_valid"
        ),
        sa.CheckConstraint(
            "dispatch_locality IN ('central','agent')",
            name="alert_channel_dispatch_locality_valid",
        ),
    )

    op.create_table(
        "alert_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("path_glob", sa.Text(), nullable=True),
        sa.Column("event_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "hash_change_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "group_by",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{event_type,library_id,rule_id}'"),
        ),
        sa.Column(
            "group_wait_s", sa.Integer(), nullable=False, server_default=sa.text("30")
        ),
        sa.Column("digest_window", sa.Text(), nullable=True),
        sa.Column("repeat_interval_s", sa.Integer(), nullable=True),
        sa.Column("threshold_count", sa.Integer(), nullable=True),
        sa.Column("threshold_window_s", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_alert_rules_library_id_libraries",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "digest_window IS NULL OR digest_window IN ('hourly','daily')",
            name="alert_rule_digest_window_valid",
        ),
    )

    op.create_table(
        "alert_rule_channels",
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.id"],
            name="fk_alert_rule_channels_rule_id_alert_rules",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channels.id"],
            name="fk_alert_rule_channels_channel_id_alert_channels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("rule_id", "channel_id"),
    )

    op.create_table(
        "alert_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "delivered", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.id"],
            name="fk_alert_events_rule_id_alert_rules",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_alert_events_item_id_items",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_alert_events_library_id_libraries",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_alert_events_pending",
        "alert_events",
        ["rule_id", "dedup_key"],
        postgresql_where=sa.text("NOT delivered"),
    )
    op.create_index(
        "ix_alert_events_rule_delivered_at",
        "alert_events",
        ["rule_id", "delivered_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_events_rule_delivered_at", table_name="alert_events")
    op.drop_index("ix_alert_events_pending", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_table("alert_rule_channels")
    op.drop_table("alert_rules")
    op.drop_table("alert_channels")
