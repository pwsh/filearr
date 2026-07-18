"""add report_definitions table (P11-T5)

Revision ID: e5f2a8c4b6d3
Revises: d7e4c1b9f3a2
Create Date: 2026-07-13 00:00:00.000000

P11-T5: persistence for CUSTOM reports. A report definition is a stored
querydsl string (``query`` — the grammar source of truth, compiled to SQL at run
time by ``filearr.query_sql``, never a stored SQL fragment) plus an ordered
column projection validated against a column registry on write.

Deliberately minimal this round (matches the phase-11 task scope): NO
``report_runs`` history table, NO scheduling columns, NO alert-channel fan-out —
those land with the background-export / scheduled-delivery tasks (later P11-T5/
T9). ``owner_principal`` is the same nullable R7 placeholder as
``saved_searches``; ``UNIQUE(owner_principal, name)`` scopes name uniqueness per
owner (the API still returns a friendly 409 on a duplicate pair).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5f2a8c4b6d3"
down_revision: str | None = "d7e4c1b9f3a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_definitions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "columns",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("sort", sa.Text(), nullable=True),
        sa.Column(
            "format", sa.Text(), nullable=False, server_default=sa.text("'csv'")
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
            "owner_principal", "name", name="uq_report_definitions_owner_name"
        ),
    )
    op.create_index(
        "ix_report_definitions_owner", "report_definitions", ["owner_principal"]
    )


def downgrade() -> None:
    op.drop_index("ix_report_definitions_owner", table_name="report_definitions")
    op.drop_table("report_definitions")
