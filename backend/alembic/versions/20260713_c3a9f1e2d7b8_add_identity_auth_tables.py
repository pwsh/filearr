"""add principals/users/service_accounts/sessions (P6-T1)

Revision ID: c3a9f1e2d7b8
Revises: a7d4f2c9e6b1
Create Date: 2026-07-13 00:00:00.000000

Phase 6 identity foundation (docs/tasks/phase-6-identity-auth-rbac-tasks.md
§ Intended DDL). ALL ADDITIVE — no existing table is touched, so a deployment
running on the API-key-only auth model upgrades with zero behaviour change until
an operator sets ``FILEARR_AUTH_ENABLED=true`` and bootstraps the first admin.

Four tables:

* ``principals``       — the abstract actor (user | service_account) + global
  role (admin | user | viewer). Soft-disable via ``disabled_at`` preserves audit
  history.
* ``users``            — human accounts. ``password_hash`` is argon2id (NULL for
  federated-only). Case-insensitive username uniqueness via a functional unique
  index on ``lower(username)`` (no ``citext`` extension dependency).
* ``service_accounts`` — first-class non-human principals that will own
  ``api_keys`` (the ApiKey backfill is a later additive migration; this ships the
  target table now).
* ``sessions``         — Postgres-backed cookie sessions. ``session_hash`` is the
  sha256 of the opaque cookie value (never the raw token); deleting a row is O(1)
  instant revocation. Indexed by principal and by (last_seen_at, expires_absolute)
  for the future inactivity/absolute-expiry sweep.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3a9f1e2d7b8"
down_revision: str | None = "a7d4f2c9e6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "global_role", sa.Text(), server_default=sa.text("'viewer'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('user','service_account')", name="principals_kind_valid"
        ),
        sa.CheckConstraint(
            "global_role IN ('admin','user','viewer')",
            name="principals_global_role_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "auth_provider", sa.Text(), server_default=sa.text("'local'"), nullable=False
        ),
        sa.Column("external_subject", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "auth_provider IN ('local','ldap','saml','oidc')",
            name="users_auth_provider_valid",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("principal_id"),
    )
    op.create_index(
        "uq_users_username_lower",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )

    op.create_table(
        "service_accounts",
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("principal_id"),
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_absolute", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "rotated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_hash"),
    )
    op.create_index("ix_sessions_principal", "sessions", ["principal_id"])
    op.create_index(
        "ix_sessions_expiry", "sessions", ["last_seen_at", "expires_absolute"]
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_expiry", table_name="sessions")
    op.drop_index("ix_sessions_principal", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("service_accounts")
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("principals")
