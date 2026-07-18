"""add libraries.enabled_presets + enabled_extension_groups (P2-T1/P2-T3)

Revision ID: c4e8a1b2f307
Revises: b7d2e4f6a891
Create Date: 2026-07-09 00:00:00.000000

Phase 2 indexing controls. Adds two ``text[]`` columns to ``libraries``, both
``NOT NULL DEFAULT '{}'`` (mirroring the existing ``enabled_types`` /
``include_globs`` / ``exclude_globs`` mappings):

  * ``enabled_presets`` (P2-T1): the library's stored preset configuration.
    Empty ``'{}'`` means "no explicit config" — effective presets are resolved at
    scan time as ``union(default_enabled bundles, stored positive entries)`` minus
    any ``-name`` negative sentinel entries (e.g. ``-hidden_dotfiles`` disables the
    shipped-on dotfile skip). Storing effective defaults would bake today's set
    into the row and block evolving the shipped-on set without a data migration —
    hence resolution-at-scan-time, not a column default.
  * ``enabled_extension_groups`` (P2-T3): finer-than-MediaType extension refinement
    (union semantics, ruling R5). Ships in THIS revision even though P2-T3 wires it,
    to avoid revision churn against a freshly-added column.

Existing rows get ``'{}'`` (server default) on upgrade: no presets stored →
``hidden_dotfiles`` default-on resolves active → today's unconditional dotfile
skip is preserved (no silent behaviour change on upgrade).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4e8a1b2f307"
down_revision: str | None = "b7d2e4f6a891"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "enabled_presets",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "libraries",
        sa.Column(
            "enabled_extension_groups",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("libraries", "enabled_extension_groups")
    op.drop_column("libraries", "enabled_presets")
