"""add items.sidecar_of self-referencing FK (T3 sidecar association)

Revision ID: a1c3f7e9b204
Revises: 2bfb3fd1d09a
Create Date: 2026-07-07 00:00:00.000000

Adds a nullable self-referencing FK on ``items`` linking a sidecar file
(.nfo / poster.jpg / -thumb.jpg / *_JRSidecar.xml, ...) to its parent media item.

``ondelete=CASCADE`` (not SET NULL): a sidecar is meaningless without its parent,
so when a parent is hard-purged from the recycle bin its orphaned sidecar rows are
removed too. Soft tombstoning (status=missing/trashed) does not trigger the FK —
it only bites at real row DELETE time, which is exactly the recycle-bin purge.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c3f7e9b204"
down_revision: str | None = "2bfb3fd1d09a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("items", sa.Column("sidecar_of", sa.UUID(), nullable=True))
    op.create_index("ix_items_sidecar_of", "items", ["sidecar_of"], unique=False)
    op.create_foreign_key(
        "fk_items_sidecar_of_items",
        "items",
        "items",
        ["sidecar_of"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_items_sidecar_of_items", "items", type_="foreignkey")
    op.drop_index("ix_items_sidecar_of", table_name="items")
    op.drop_column("items", "sidecar_of")
