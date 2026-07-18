"""add saved_searches table (P3-T7)

Revision ID: e2f4a6c8b0d1
Revises: d9a1c4e6b8f3
Create Date: 2026-07-12 00:00:00.000000

P3-T7 (brief §7 P1): named, persisted ``/search`` queries. Pure Postgres — the
table never touches Meilisearch (invariant 1: trivially rebuild-compatible; a
saved search is just a stored bundle of the flat ``/search`` params, replayed by
re-running the endpoint).

``owner_principal`` is a nullable R7 placeholder from day one: the column exists
now so phase-6 (identity/auth/RBAC) can start enforcing per-owner ACLs without a
later migration. ``UNIQUE(owner_principal, name)`` gives per-owner name
uniqueness; Postgres treats each NULL owner as distinct, so two anonymous saves
with the same name would NOT collide at the DB level — the API layer still
returns a friendly 409 for a duplicate (owner, name) pair on the current single-
principal deployment.

``params`` is the typed ``/search`` query bundle stored verbatim as JSONB. The
API validates every key against ``filearr.api.search.SEARCH_PARAM_NAMES`` (derived
from the endpoint signature) before it ever lands here, so an unknown/renamed
param is a 422, not a silently-stored dead key.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2f4a6c8b0d1"
down_revision: str | None = "d9a1c4e6b8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.UniqueConstraint(
            "owner_principal", "name", name="uq_saved_searches_owner_name"
        ),
    )
    op.create_index(
        "ix_saved_searches_owner", "saved_searches", ["owner_principal"]
    )


def downgrade() -> None:
    op.drop_index("ix_saved_searches_owner", table_name="saved_searches")
    op.drop_table("saved_searches")
