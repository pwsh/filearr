"""add scan_paths table + scan_runs.rel_path scope (P2-T6)

Revision ID: d5f9a3c1e8b0
Revises: c4e8a1b2f307
Create Date: 2026-07-09 00:00:00.000000

Phase 2 indexing controls, P2-T6 (hot-folder scheduling / per-subfolder scan
cadence + watch overrides). Two changes, both additive:

  * ``scan_paths`` (brief §3.6): one row per governed subfolder of a library.
    ``rel_path`` is relative to ``libraries.root_path`` (``''`` = the library
    root itself), matching the ``items.rel_path`` identity convention
    (invariant 3). ``scan_cron`` / ``watch_mode`` are NULL-inherits-from-library
    overrides (T7's ``hash_full_max_bytes`` convention). ``UNIQUE(library_id,
    rel_path)`` makes each subfolder configurable exactly once; the FK CASCADEs
    so deleting a library drops its scan_paths rows.
  * ``scan_runs.rel_path`` (nullable): the subtree scope a scoped scan ran
    against. ``NULL`` = a full-library scan (today's behaviour). Used by the
    scheduler's running-scan detection to distinguish a running FULL scan (which
    a scoped defer must skip behind) from a running scoped scan.

A library with zero ``scan_paths`` rows behaves exactly as T5 (regression
guard): the scheduler defers only the library-level ``scan_cron`` and the watch
supervisor runs only the library-root watcher.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5f9a3c1e8b0"
down_revision: str | None = "c4e8a1b2f307"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_paths",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "libraries.id",
                ondelete="CASCADE",
                name="fk_scan_paths_library_id_libraries",
            ),
            nullable=False,
        ),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("scan_cron", sa.Text(), nullable=True),
        sa.Column("watch_mode", sa.Boolean(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("library_id", "rel_path", name="uq_scan_paths_library_rel_path"),
    )
    op.create_index("ix_scan_paths_library_id", "scan_paths", ["library_id"])
    # Scope column on scan_runs: NULL = full-library scan; a rel_path = the
    # subtree a scoped (hot-folder) scan ran against.
    op.add_column(
        "scan_runs", sa.Column("rel_path", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("scan_runs", "rel_path")
    op.drop_index("ix_scan_paths_library_id", table_name="scan_paths")
    op.drop_table("scan_paths")
