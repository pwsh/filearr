"""add items.last_verified_at (agent stat/rehash verification) — P10-T3

Revision ID: b7e3d1f9a2c4
Revises: a3f7c1e9b5d4
Create Date: 2026-07-17 16:00:00.000000

Phase 10, P10-T3 (agent stat_check / rehash_check verification flow). One
additive, nullable column on ``items``:

  * ``last_verified_at TIMESTAMPTZ NULL`` — the instant an agent last confirmed
    (via a ``stat_check`` / ``rehash_check`` command) that this agent-hosted
    item still exists / is unchanged. NULL = never verified. Stamped by the
    verify-completion reconcile path (``filearr.verify``); surfaced in the item
    detail UI as a "last verified <relative>" freshness line for agent-owned
    items. Only ever set for items whose library is agent-owned
    (``libraries.source_agent_id``); a centrally-scanned item keeps NULL.

Purely additive (a new nullable column, no backfill); the downgrade drops it.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e3d1f9a2c4"
down_revision: str | None = "a3f7c1e9b5d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "last_verified_at")
