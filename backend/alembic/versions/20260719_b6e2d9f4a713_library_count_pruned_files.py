"""libraries.count_pruned_files — opt-in reconciliation of pruned scan subtrees

Revision ID: b6e2d9f4a713
Revises: d3f8a1c6e2b5
Create Date: 2026-07-19 20:10:00.000000

A scan could not explain its own numbers. Live case (2026-07-19): a library
reported ``seen 77,394`` + ``excluded 318`` against 99,694 files on disk. The
missing 21,978 were all inside dot-directories (``.git`` / ``.venv``) pruned
wholesale by the default-on ``hidden_dotfiles`` preset — and because pruning
skips a tree WITHOUT enumerating it, those files were counted nowhere and no
number in the UI could account for them.

``count_pruned_files`` opts a library into a second, deliberately cheap pass over
each pruned subtree (``scandir`` only — no ``stat``, no gitignore matching, no
ingestion) purely to count what was skipped, so::

    seen + excluded + pruned_files == files on disk

Default **false**: that pass is a full directory listing of trees we have chosen
not to index, which is precisely the expensive operation on the rclone/SMB mounts
where big pruned trees live. Operators turn it on when a library's item count
disagrees with the OS, then usually turn it back off.

Additive boolean with a server default, so existing rows need no backfill.
"""

import sqlalchemy as sa

from alembic import op

revision = "b6e2d9f4a713"
down_revision = "d3f8a1c6e2b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "count_pruned_files",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("libraries", "count_pruned_files")
