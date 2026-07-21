"""add File Extension Similarity Taxonomy tables + items.file_category/file_group (W8-A)

Revision ID: a1f4c7e2b9d3
Revises: c7d9e1f3a5b8
Create Date: 2026-07-18 20:00:00.000000

Wave 8, W8-A — the DB-backed, editable File Extension Similarity Taxonomy: the
keystone foundation that REPLACES ``media_type`` (a later unit, W8-B, removes
media_type and routes extraction off the category ``extractor``; W8-C ships the
frontend editor). This unit is ADDITIVE and keeps the suite green — media_type
stays as a derived alias.

Four new tables (uuidv7 PKs), SEEDED in this migration FROM
``filearr.file_groups`` (the 37-group / 1271-ext research map, now the seed source
of truth):

  * ``file_categories`` — the ~9 coarse parents (image/audio/video/document/
    three-d-cad/development/archive/system/other), each with an ``extractor``
    (image/audio/video/document/model3d or NULL) W8-B routes on. ``key`` UNIQUE.
  * ``file_groups`` — the 37 finer children; ``category_id`` FK ON DELETE RESTRICT
    (a category cannot be deleted while it parents groups). ``key`` UNIQUE.
  * ``file_group_extensions`` — the ext→group membership; ``ext`` UNIQUE lowercase
    (an ext belongs to one group), ``group_id`` FK ON DELETE CASCADE.
  * ``taxonomy_state`` — one row (id=1) holding an integer ``version`` bumped on
    every CRUD edit (cache invalidation now, agent push later). Seeded version=1.

Plus two additive nullable columns on ``items`` — ``file_category`` /
``file_group`` — each indexed. New scans/replication populate them (via
``filearr.taxonomy``); existing rows stay NULL (no backfill — a dev DB wipe is
acceptable, R). The downgrade drops the columns + all four tables.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1f4c7e2b9d3"
down_revision: str | None = "c7d9e1f3a5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _seed(conn) -> None:
    """Seed the three taxonomy tables + taxonomy_state FROM filearr.file_groups.

    The migration is the moment the pure research map becomes editable DB rows; the
    seed is the DEFAULT, edits happen at runtime through the CRUD API."""
    from filearr.file_groups import taxonomy_seed_payload

    payload = taxonomy_seed_payload()

    for cat in payload["categories"]:
        conn.execute(
            sa.text(
                "INSERT INTO file_categories "
                "(key, label, description, extractor, sort_order, is_builtin) "
                "VALUES (:key, :label, :description, :extractor, :sort_order, true)"
            ),
            cat,
        )
    cat_ids = {
        row.key: row.id
        for row in conn.execute(sa.text("SELECT key, id FROM file_categories"))
    }

    for grp in payload["groups"]:
        conn.execute(
            sa.text(
                "INSERT INTO file_groups "
                "(key, label, description, category_id, sort_order, is_builtin) "
                "VALUES (:key, :label, :description, :category_id, :sort_order, true)"
            ),
            {
                "key": grp["key"],
                "label": grp["label"],
                "description": grp["description"],
                "category_id": cat_ids[grp["category"]],
                "sort_order": grp["sort_order"],
            },
        )
    grp_ids = {
        row.key: row.id
        for row in conn.execute(sa.text("SELECT key, id FROM file_groups"))
    }

    for row in payload["extensions"]:
        conn.execute(
            sa.text(
                "INSERT INTO file_group_extensions (ext, group_id) "
                "VALUES (:ext, :group_id)"
            ),
            {"ext": row["ext"], "group_id": grp_ids[row["group"]]},
        )

    conn.execute(
        sa.text("INSERT INTO taxonomy_state (id, version) VALUES (1, 1)")
    )


def upgrade() -> None:
    op.create_table(
        "file_categories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("extractor", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
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
        sa.UniqueConstraint("key", name="uq_file_categories_key"),
    )

    op.create_table(
        "file_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
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
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["file_categories.id"],
            name="fk_file_groups_category_id_file_categories",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("key", name="uq_file_groups_key"),
    )
    op.create_index("ix_file_groups_category", "file_groups", ["category_id"])

    op.create_table(
        "file_group_extensions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("ext", sa.Text(), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["file_groups.id"],
            name="fk_file_group_extensions_group_id_file_groups",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("ext", name="uq_file_group_extensions_ext"),
    )

    op.create_table(
        "taxonomy_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Additive item columns (nullable; populated by new scans/replication).
    op.add_column("items", sa.Column("file_category", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("file_group", sa.Text(), nullable=True))
    op.create_index("ix_items_file_category", "items", ["file_category"])
    op.create_index("ix_items_file_group", "items", ["file_group"])

    _seed(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_items_file_group", table_name="items")
    op.drop_index("ix_items_file_category", table_name="items")
    op.drop_column("items", "file_group")
    op.drop_column("items", "file_category")
    op.drop_table("taxonomy_state")
    op.drop_table("file_group_extensions")
    op.drop_index("ix_file_groups_category", table_name="file_groups")
    op.drop_table("file_groups")
    op.drop_table("file_categories")
