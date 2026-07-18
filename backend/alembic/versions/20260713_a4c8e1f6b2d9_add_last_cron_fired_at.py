"""add last_cron_fired_at to libraries + scan_paths (FIX-8 scan-scheduling storm)

Revision ID: a4c8e1f6b2d9
Revises: e5f2a8c4b6d3
Create Date: 2026-07-13 14:40:00.000000

FIX-8 (scan-scheduling storm): once-per-occurrence cron firing. The minute
scheduler tick (``worker.schedule_scans``) previously deferred a scan on the
EXACT cron minute only, with the "already running" guard keyed on live ScanRun
rows + the partial ``queueing_lock`` (``todo`` only). When a worker died mid-scan
(OOM under concurrent SMB scans) BEFORE its ScanRun row committed, neither guard
saw the stalled ``doing`` job, so every subsequent due tick re-deferred -- 5-6
duplicate scan jobs stacked per library.

The persisted marker fixes the "fire once per occurrence" half of the fix: the
tick now computes the latest cron occurrence at/before now and fires only when it
is strictly newer than ``last_cron_fired_at``, stamping the occurrence in the
same commit as the enqueue (at-most-once per occurrence -- see
``schedule.due_occurrence``). NULL (the upgrade default for existing rows) means
"never fired": a schedule fires only from its NEXT occurrence, never a backfilled
catch-up -- a safe, non-destructive default that preserves prior first-fire
behaviour.

Added to BOTH ``libraries`` (full-library schedule) and ``scan_paths`` (P2-T6
hot-folder scoped schedule) so scoped schedules get the identical guarantee.
Nullable, no default -- a pure additive column; downgrade drops both.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4c8e1f6b2d9"
down_revision: str | None = "e5f2a8c4b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("last_cron_fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scan_paths",
        sa.Column("last_cron_fired_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scan_paths", "last_cron_fired_at")
    op.drop_column("libraries", "last_cron_fired_at")
