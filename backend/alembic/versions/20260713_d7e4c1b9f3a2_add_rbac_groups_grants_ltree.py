"""add RBAC groups + path_grants + ltree scopes (P6-T2)

Revision ID: d7e4c1b9f3a2
Revises: c3a9f1e2d7b8
Create Date: 2026-07-13 00:00:00.000000

Phase 6 RBAC core (docs/tasks/phase-6-identity-auth-rbac-tasks.md § P6-T2 +
P6-T2a directives). ALL ADDITIVE — a deployment on the API-key-only auth model
upgrades with zero behaviour change; effective permissions only ever NARROW once
an operator creates a group + a ``path_grants`` row (default post-migration
state: every principal keeps its global-role reach everywhere).

Adds:

* the ``ltree`` extension (guarded — see below),
* ``principal_groups`` / ``principal_group_members`` (the RBAC grouping unit +
  its membership edges),
* ``path_grants`` (path-scoped ACL grants — ``subject_kind`` principal|group,
  ``scope`` ltree, single ``action`` from ``rbac.ACTIONS``, ``effect``
  allow|deny) with a GIST ancestor index on ``scope``,
* ``items.path_scope`` (the ltree scope key each item lives under) + its GIST
  index.

**ltree availability guard.** ``contrib`` (which ships ``ltree``) is present in
the official ``postgres:18`` image this project targets, so production always
gets the real ``ltree`` column type + native GIST ``<@``/``@>`` ancestor index.
Some minimal / bundled Postgres builds (e.g. the ``pgserver`` used by the test
sandbox) omit ``contrib`` entirely. Rather than fail the whole migration there,
we detect availability via ``pg_available_extensions`` and fall back to a plain
``text`` column + a ``text_pattern_ops`` btree index. The stored value is the
SAME dotted string in both cases (``rbac.path_to_ltree`` output); only the
column *type* and index *kind* differ. RBAC evaluation this round is the pure
Python ``rbac.evaluate`` (ancestor-matching in Python), so correctness does not
depend on the DB-side ltree operators — the ltree type + GIST index are the
forward-looking acceleration for P6-T3/T4's indexed ancestor queries.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7e4c1b9f3a2"
down_revision: str | None = "c3a9f1e2d7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class _Ltree(sa.types.UserDefinedType):
    """Minimal ``ltree`` column type for DDL emission (no Python-side coercion —
    values bind/read as plain ``str``)."""

    cache_ok = True

    def get_col_spec(self, **kw):  # noqa: ANN001, ANN201, ANN003
        return "ltree"


def _ltree_available(conn) -> bool:  # noqa: ANN001
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'ltree'")
    ).first()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()
    has_ltree = _ltree_available(conn)
    if has_ltree:
        op.execute("CREATE EXTENSION IF NOT EXISTS ltree")

    scope_type: sa.types.TypeEngine = _Ltree() if has_ltree else sa.Text()

    op.create_table(
        "principal_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'local'"), nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('local','ldap','saml','oidc')",
            name="principal_groups_source_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_principal_groups_name"),
    )

    op.create_table(
        "principal_group_members",
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["principal_groups.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("principal_id", "group_id"),
    )
    op.create_index(
        "ix_principal_group_members_group", "principal_group_members", ["group_id"]
    )

    op.create_table(
        "path_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", scope_type, nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), server_default=sa.text("'allow'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "subject_kind IN ('principal','group')",
            name="path_grants_subject_kind_valid",
        ),
        sa.CheckConstraint(
            "effect IN ('allow','deny')", name="path_grants_effect_valid"
        ),
        sa.CheckConstraint(
            "action IN ('search_metadata','search_content','download','upload',"
            "'modify','delete','edit_metadata','manage_alerts')",
            name="path_grants_action_valid",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["libraries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_path_grants_subject", "path_grants", ["subject_kind", "subject_id"])
    op.create_index("ix_path_grants_library", "path_grants", ["library_id"])

    op.add_column("items", sa.Column("path_scope", scope_type, nullable=True))

    if has_ltree:
        # Native ltree GIST ancestor index (``scope @> item.path_scope``).
        op.execute(
            "CREATE INDEX ix_path_grants_scope_gist ON path_grants USING GIST (scope)"
        )
        op.execute(
            "CREATE INDEX ix_items_path_scope_gist ON items USING GIST (path_scope)"
        )
    else:
        # Extension-free fallback: prefix-scannable btree.
        op.create_index(
            "ix_path_grants_scope",
            "path_grants",
            ["scope"],
            postgresql_ops={"scope": "text_pattern_ops"},
        )
        op.create_index(
            "ix_items_path_scope",
            "items",
            ["path_scope"],
            postgresql_ops={"path_scope": "text_pattern_ops"},
        )


def downgrade() -> None:
    conn = op.get_bind()
    has_ltree = _ltree_available(conn)
    if has_ltree:
        op.execute("DROP INDEX IF EXISTS ix_items_path_scope_gist")
        op.execute("DROP INDEX IF EXISTS ix_path_grants_scope_gist")
    else:
        op.drop_index("ix_items_path_scope", table_name="items")
        op.drop_index("ix_path_grants_scope", table_name="path_grants")
    op.drop_column("items", "path_scope")
    op.drop_index("ix_path_grants_library", table_name="path_grants")
    op.drop_index("ix_path_grants_subject", table_name="path_grants")
    op.drop_table("path_grants")
    op.drop_index(
        "ix_principal_group_members_group", table_name="principal_group_members"
    )
    op.drop_table("principal_group_members")
    op.drop_table("principal_groups")
    # Leave the ltree extension in place — other objects may come to depend on it
    # and dropping a shared extension on downgrade is riskier than leaving it.
