"""add libraries.share_prefix + items rel_path text_pattern_ops index (UI-T12)

Revision ID: f8b3c1d05a29
Revises: e7c2b9a4d6f1
Create Date: 2026-07-10 12:00:00.000000

UI-T12 (path breadcrumbs / network-open links / in-page folder navigation). Two
additive changes:

  * ``libraries.share_prefix`` (nullable TEXT): the USER-facing network location
    of the library root (e.g. ``\\\\tower\\media``, ``smb://tower/media``,
    ``/Volumes/media``). Distinct from ``native_prefix`` (the *source-system*
    path used for *arr-style remote path mapping): ``share_prefix`` is what a
    human types into a file manager / Finder to open the file, and drives the
    "Open via network" affordances + copy-path display in the UI.
  * ``ix_items_library_rel_path_pattern``: a btree index on
    ``(library_id, rel_path text_pattern_ops)``. The existing
    ``ix_items_library_rel_path`` unique index uses the default (collation-aware)
    opclass, which does NOT accelerate anchored ``LIKE 'prefix/%'`` scans under a
    non-C collation. The folder-browse endpoint (P3 in-page navigation) leans on
    exactly such prefix scans over ``rel_path``; ``text_pattern_ops`` makes them
    index-served at 1M rows regardless of the DB collation.

A database that never queries the tree endpoint is unaffected (the extra index
only adds write cost proportional to one more btree). ``share_prefix`` defaults
to NULL, so existing libraries behave exactly as before (no open-location
affordances until an operator sets it).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f8b3c1d05a29"
down_revision: str | None = "e7c2b9a4d6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("share_prefix", sa.Text(), nullable=True))
    # text_pattern_ops so anchored LIKE 'prefix/%' folder-browse scans are
    # index-served irrespective of the database's default collation.
    op.create_index(
        "ix_items_library_rel_path_pattern",
        "items",
        ["library_id", "rel_path"],
        postgresql_ops={"rel_path": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_items_library_rel_path_pattern", table_name="items")
    op.drop_column("libraries", "share_prefix")
