"""add agents + enrollment_tokens (P5-T1 distributed-agent enrollment)

Revision ID: b4d8f0a2c6e4
Revises: a2b4c6d8e0f2
Create Date: 2026-07-16 00:00:00.000000

Phase 5 wave opener: the central-side trust anchor for the distributed agent
fleet. ``agents`` is the server-assigned identity registry (R3 — the id is
minted here, never client-chosen, and is what the agent embeds in its cert
CN/SAN); ``enrollment_tokens`` are single-use, short-TTL, hashed-at-rest tokens
presented once (R3) to consume at ``/agents/register``.

The agent-local outbox/index and the ``agent_replication_log`` idempotency
ledger are LATER tasks (P5-T3/T4) and are deliberately NOT created here.

DDL deviation (documented, forced by R3): the research brief §7.2 types
``agents.cert_fingerprint`` as ``NOT NULL UNIQUE``. R3 mandates
register-before-cert, so no fingerprint exists at registration time. The column
is therefore nullable with a PARTIAL unique index (unique only among bound
fingerprints); a freshly-registered agent is "pending" until P5-T2's
agent↔step-ca flow binds the fingerprint. ``enroll_secret_hash`` is a P5-T1
addition beyond the brief DDL: a one-time hashed nonce that gates the
fingerprint-binding call so a guessed pending-agent UUID cannot be hijacked
(hardened by mTLS in P5-T2).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4d8f0a2c6e4"
down_revision: str | None = "a2b4c6d8e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column(
            "rollout_group",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("cert_fingerprint", sa.Text(), nullable=True),
        sa.Column("enroll_secret_hash", sa.Text(), nullable=True),
        sa.Column(
            "last_contiguous_seq_no",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("policy_version_applied", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "platform IN ('windows','macos','linux')", name="agent_platform_valid"
        ),
    )
    op.create_index("ix_agents_rollout_group", "agents", ["rollout_group"])
    # Partial unique index: identity binding is unique only among BOUND certs;
    # multiple pending agents (NULL fingerprint) coexist (R3 register-before-cert).
    op.create_index(
        "ix_agents_cert_fingerprint",
        "agents",
        ["cert_fingerprint"],
        unique=True,
        postgresql_where=sa.text("cert_fingerprint IS NOT NULL"),
    )

    op.create_table(
        "enrollment_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "rollout_group",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by"], ["agents.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_enrollment_tokens_expires", "enrollment_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_enrollment_tokens_expires", table_name="enrollment_tokens")
    op.drop_table("enrollment_tokens")
    op.drop_index("ix_agents_cert_fingerprint", table_name="agents")
    op.drop_index("ix_agents_rollout_group", table_name="agents")
    op.drop_table("agents")
