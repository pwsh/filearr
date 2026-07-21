"""media_type CUTOVER: drop the MediaType enum/column, taxonomy-gate libraries (W8-B)

Revision ID: d3f8a1c6e2b5
Revises: a1f4c7e2b9d3
Create Date: 2026-07-18 21:30:00.000000

Wave 8, W8-B — the CUTOVER that removes the legacy ``media_type`` enum entirely and
routes classification / extraction / library gating off the DB-backed File Extension
Similarity Taxonomy that W8-A shipped. USER RULING: fresh FULL redeployment — this
revision defines the TARGET schema directly, with NO data-preserving/backfill logic
(``items.file_category`` / ``items.file_group`` are already populated at scan/
replication time by ``filearr.taxonomy``).

Structural changes:

  * ``items`` — drop the ``media_type`` column, its ``ix_items_media_type`` index,
    and the ``media_type`` Postgres enum TYPE. ``file_category`` / ``file_group``
    (added additive in W8-A) are now authoritative.
  * ``libraries`` — replace the MediaType-keyed ``enabled_types`` with the two
    taxonomy-gating arrays ``enabled_categories`` (rename of ``enabled_types``) +
    ``enabled_groups`` (new). A file is included iff its file_group is in
    enabled_groups OR its file_category is in enabled_categories; BOTH empty = all.
  * ``metadata_profiles`` — rename the ``media_type`` key column to ``file_category``
    (profiles are re-keyed off the taxonomy category; ``filearr.profiles``).

The downgrade is a clean STRUCTURAL revert (no data preservation, per ruling): it
re-creates the enum + a NULLABLE ``items.media_type`` column, splits the gating
columns back to ``enabled_types``, and renames the profile key back.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f8a1c6e2b5"
down_revision: str | None = "a1f4c7e2b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- items: drop media_type (column + index + enum type) -----------------
    op.drop_index("ix_items_media_type", table_name="items")
    op.drop_column("items", "media_type")
    sa.Enum(name="media_type").drop(op.get_bind(), checkfirst=True)

    # --- libraries: enabled_types -> enabled_categories + enabled_groups ------
    op.alter_column(
        "libraries",
        "enabled_types",
        new_column_name="enabled_categories",
        existing_type=sa.ARRAY(sa.Text()),
        existing_nullable=False,
    )
    op.add_column(
        "libraries",
        sa.Column(
            "enabled_groups",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # --- metadata_profiles: media_type key -> file_category -------------------
    op.alter_column(
        "metadata_profiles",
        "media_type",
        new_column_name="file_category",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE metadata_profiles "
        "RENAME CONSTRAINT uq_metadata_profiles_media_type "
        "TO uq_metadata_profiles_file_category"
    )


def downgrade() -> None:
    # --- metadata_profiles: file_category -> media_type -----------------------
    op.execute(
        "ALTER TABLE metadata_profiles "
        "RENAME CONSTRAINT uq_metadata_profiles_file_category "
        "TO uq_metadata_profiles_media_type"
    )
    op.alter_column(
        "metadata_profiles",
        "file_category",
        new_column_name="media_type",
        existing_type=sa.Text(),
        existing_nullable=False,
    )

    # --- libraries: enabled_categories/enabled_groups -> enabled_types --------
    op.drop_column("libraries", "enabled_groups")
    op.alter_column(
        "libraries",
        "enabled_categories",
        new_column_name="enabled_types",
        existing_type=sa.ARRAY(sa.Text()),
        existing_nullable=False,
    )

    # --- items: recreate the media_type enum + a (nullable) column + index ----
    media_type = sa.Enum(
        "video", "audio", "audiobook", "sample", "image", "model3d",
        "document", "spreadsheet", "other",
        name="media_type",
    )
    media_type.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "items",
        sa.Column("media_type", media_type, nullable=True),
    )
    op.create_index("ix_items_media_type", "items", ["media_type"], unique=False)
