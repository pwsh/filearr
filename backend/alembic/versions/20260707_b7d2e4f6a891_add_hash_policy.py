"""add libraries.hash_policy + hash_full_max_bytes (T7 hash policy)

Revision ID: b7d2e4f6a891
Revises: a1c3f7e9b204
Create Date: 2026-07-07 00:00:00.000000

Per-library control over the expensive full ``content_hash`` (whole-file xxh3),
the pain point for multi-GB video over SMB/NFS.

  * ``hash_policy`` (text, NOT NULL, default 'auto'): 'auto' | 'full' |
    'quick_only'. Stored as text rather than a Postgres enum so a future policy
    value needs no ``ALTER TYPE``; the value set is enforced at the API boundary
    against ``filearr.models.HashPolicy``.
  * ``hash_full_max_bytes`` (bigint, nullable): per-library override of the global
    ``FILEARR_SCAN_HASH_FULL_MAX_BYTES`` ceiling. NULL -> use the global value.

Existing rows get 'auto' (server default) on upgrade, which preserves prior
behaviour for local libraries (auto+local == full) and only *reduces* IO for
network libraries (auto+network -> quick_only) -- a safe, non-destructive change.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d2e4f6a891"
down_revision: str | None = "a1c3f7e9b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "hash_policy",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
    )
    op.add_column(
        "libraries",
        sa.Column("hash_full_max_bytes", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("libraries", "hash_full_max_bytes")
    op.drop_column("libraries", "hash_policy")
