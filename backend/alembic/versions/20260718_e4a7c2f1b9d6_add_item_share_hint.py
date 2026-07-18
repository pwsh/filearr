"""add items.share_hint (agent share-location discovery) — P10-T11

Revision ID: e4a7c2f1b9d6
Revises: d2f4b6a8c0e1
Create Date: 2026-07-18 12:00:00.000000

Phase 10, P10-T11 (agent share-location discovery). One additive, nullable
column on ``items``:

  * ``share_hint JSONB NULL`` — the best-effort network-share hint an agent
    reports for an item, stored verbatim as the additive replication-event
    ``share_hint`` object (``{share_url, unc, share_name, host, source:"agent"}``).
    Set by ``agentsync.apply_batch`` on the UPSERT path only (create stamps it
    verbatim; a modified event refreshes it only when it carries a hint, so a
    hint-less modified event does not clobber a prior good hint); tombstones /
    deletes never touch it, and full-reconciliation (``reconcile_finish``) does
    NOT carry hints (the manifest shape is frozen — hints refresh via incremental
    replication only). NULL is the NORMAL case (R1: discovery is advisory —
    anonymous shares, permission-scoped enumeration, and multi-homed hosts mean
    most agents report nothing) and the item then falls through to the central
    ``agent_share_maps`` mapping / library ``share_prefix`` (P10-T12). Never set
    for a centrally-scanned item. Stored as opaque JSONB (not typed columns) so
    the wire shape stays additive + versionable.

Purely additive (a new nullable column, no backfill); the downgrade drops it.
"""
from collections.abc import Sequence

from sqlalchemy.dialects import postgresql

import sqlalchemy as sa

from alembic import op

revision: str = "e4a7c2f1b9d6"
down_revision: str | None = "d2f4b6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("share_hint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "share_hint")
