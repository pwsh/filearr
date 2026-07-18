"""add thumbnail_manifest (S12/P12 slice 1 thumbnails)

Revision ID: a7d4f2c9e6b1
Revises: b5e8d3a1c7f4
Create Date: 2026-07-12 18:00:00.000000

Content-addressed thumbnail cache manifest (research §7 DDL, reconciled with the
S12 task schema). A DISPOSABLE projection: it INDEXES filesystem-resident WebP
thumbnails so GC/staleness checks are cheap Postgres queries instead of
million-entry directory walks -- the filesystem holds the bytes, not this table,
and every row is rebuildable from the still-live source via ``thumb_item``.

Columns:
  * ``item_id``   FK items(id) ON DELETE CASCADE -- a hard recycle-bin purge
    (invariant 4) removes the row automatically; the on-disk file is reclaimed by
    the next GC sweep (a sweep, not a synchronous cascade delete, so a Postgres
    transaction never blocks on filesystem I/O).
  * ``tier``      smallint -- 0=grid (320px), 1=preview (800px).
  * ``cache_key`` text -- blake2b(hash:generator_version:tier) hex; the on-disk
    path is derived entirely from this digest. NOT unique: two items with
    byte-identical content share one cache_key (and one file) -- the cross-item
    dedup the content-addressed scheme gives for free.
  * ``bytes``     encoded WebP size (drives the storage-budget stats + GC counters).
  * ``width``/``height`` smallint -- final thumbnail dimensions.
  * ``source``    'artwork' (sidecar poster/thumb, rule 0) | 'image' | 'audio_embedded'
    | 'video' (slice 2). Provenance for debugging / future policy.
  * ``generated_at`` when this row was (re)written.

Constraints/indexes:
  * ``UNIQUE(item_id, tier)`` -- the upsert key; a regeneration REPLACES the row
    (old cache_key's file becomes an orphan the GC file-walk reclaims).
  * ``ix_thumbnail_manifest_cache_key`` -- GC's file-orphan lookup ("is this
    on-disk key still referenced?").

Downgrade drops the table (nothing outside this migration references it).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7d4f2c9e6b1"
down_revision: str | None = "b5e8d3a1c7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thumbnail_manifest",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.SmallInteger(), nullable=True),
        sa.Column("height", sa.SmallInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("item_id", "tier", name="uq_thumbnail_manifest_item_tier"),
    )
    op.create_index(
        "ix_thumbnail_manifest_cache_key", "thumbnail_manifest", ["cache_key"]
    )
    op.create_index("ix_thumbnail_manifest_item", "thumbnail_manifest", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_thumbnail_manifest_item", table_name="thumbnail_manifest")
    op.drop_index("ix_thumbnail_manifest_cache_key", table_name="thumbnail_manifest")
    op.drop_table("thumbnail_manifest")
