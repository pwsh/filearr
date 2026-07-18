"""add metadata_profiles + custom_fields tables + user_metadata GIN/CHECK (P4)

Revision ID: e7c2b9a4d6f1
Revises: d5f9a3c1e8b0
Create Date: 2026-07-10 00:00:00.000000

Phase 4 data-model extensions, combined into ONE revision so the P4 schema
settles without per-task revision churn (matches the P2-T1 "ship the column with
its revision" precedent). Four additive changes:

  * ``metadata_profiles`` (P4-T1): code-shipped, versioned, ``MediaType``-keyed
    schemas describing the well-known fields each extractor emits. Seeded/upserted
    at startup from ``filearr.profiles.METADATA_PROFILES``; migrations only ADD
    rows or bump ``version`` (R2). ``media_type`` UNIQUE = the upsert target.
  * ``custom_fields`` (P4-T3 *table*; CRUD is a later task): Paperless-shaped
    admin-defined field definitions whose values live only in
    ``Item.user_metadata``. The table ships now so the P4 schema is one revision;
    ``data_type`` / ``name`` immutability is enforced at the API layer (P4-T3),
    not a DB trigger (keep the "why" a 422).
  * ``ix_items_user_metadata`` GIN index (P4-T5): custom-field filter performance
    over the user-edit overlay.
  * ``user_metadata_is_object`` CHECK (P4-T5): a native, no-extension
    defense-in-depth guard (``jsonb_typeof(user_metadata) = 'object'``) — R4
    rejected ``pg_jsonschema`` (unverified license + native-extension cost); this
    is the only DB-side backstop.

A database with zero ``metadata_profiles`` / ``custom_fields`` rows behaves
exactly as before (regression guard): profiles are seeded at startup and the
custom-field CRUD/validation/projection paths are all no-ops on an empty table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7c2b9a4d6f1"
down_revision: str | None = "d5f9a3c1e8b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_profiles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("media_type", name="uq_metadata_profiles_media_type"),
    )

    op.create_table(
        "custom_fields",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("select_options", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "applies_to",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "library_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "facetable", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "sortable", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "required", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_custom_fields_name"),
    )

    # P4-T5: GIN over the user-edit overlay + a structural guard that
    # user_metadata is always a JSON object (native jsonb_typeof, no extension).
    op.create_index(
        "ix_items_user_metadata",
        "items",
        ["user_metadata"],
        postgresql_using="gin",
    )
    op.create_check_constraint(
        "user_metadata_is_object",
        "items",
        "jsonb_typeof(user_metadata) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("user_metadata_is_object", "items", type_="check")
    op.drop_index("ix_items_user_metadata", table_name="items")
    op.drop_table("custom_fields")
    op.drop_table("metadata_profiles")
