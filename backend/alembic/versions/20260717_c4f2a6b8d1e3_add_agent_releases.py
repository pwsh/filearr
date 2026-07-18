"""add agent_releases (P5-T7 signed update manifest + staged rollout)

Revision ID: c4f2a6b8d1e3
Revises: b7e3d1f9a2c4
Create Date: 2026-07-17 18:00:00.000000

Phase 5, P5-T7 (signed agent self-update). One append-mostly table storing each
uploaded release's SIGNED manifest and its rollout stage:

  * ``version``      TEXT UNIQUE — the release version (semver-ish); one row per
    version, uploaded once, then promoted in place.
  * ``stage``        TEXT — ``'canary'`` (initial upload, seen only by agents in
    the canary rollout_group) or ``'general'`` (promoted; seen by every agent).
    The operator-confirmation gate (R5 / research §6.3) is the canary→general
    promote.
  * ``manifest``     JSONB — the manifest EXACTLY as signed, INCLUDING the
    Ed25519 ``signature`` field. Central STORES but cannot re-sign it: the agent
    verifies the signature against its build-time pinned public key (research §8
    threat model — central is untrusted for update integrity). Storing as JSONB
    is safe because the agent re-derives the canonical signed bytes from the
    parsed fields, not from central's byte layout.
  * ``created_at`` / ``promoted_at`` TIMESTAMPTZ — upload + promote instants.

Artifact BINARIES do NOT live in this table — they are written to
``FILEARR_AGENT_RELEASES_DIR`` (default ``{config_dir}/agent-releases/<version>/``)
and served by an agent-authed download endpoint. Purely additive; no backfill.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4f2a6b8d1e3"
down_revision: str | None = "b7e3d1f9a2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_releases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'canary'"),
        ),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('canary','general')", name="agent_releases_stage_valid"
        ),
        sa.UniqueConstraint("version", name="uq_agent_releases_version"),
    )
    # Newest-covering-release lookup (agent-plane manifest GET) + admin list.
    op.create_index(
        "ix_agent_releases_stage_created",
        "agent_releases",
        ["stage", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_releases_stage_created", table_name="agent_releases")
    op.drop_table("agent_releases")
