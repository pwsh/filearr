"""staging_transfers dedup index + activity watermark (P10-T6 race fix, P10-T8 sweep)

Revision ID: d2f4b6a8c0e1
Revises: d1e5b9c3a7f2
Create Date: 2026-07-18 10:00:00.000000

Two additive alterations of ``staging_transfers``, landing together (one head
bump) because both are single-table changes serving the same retrieve-UX slice:

**P10-T6 — partial UNIQUE index ``uq_staging_transfers_active_item``.** The
architect race-fix ruling carried from the T13 review: initiate's duplicate-
active guard is check-then-insert, so two concurrent retrieves of the SAME item
can both pass the SELECT and insert two active transfers. A partial UNIQUE index
on ``item_id`` restricted to the ACTIVE states (``pending``/``uploading``/
``staged``) closes that at the DB — the loser's INSERT raises ``IntegrityError``,
which the API converts to the SAME 409-with-existing-id contract the non-racing
path already returns. A transfer that reaches a terminal state
(``downloaded``/``expired``/``failed``) leaves the index, so the item is freely
retrievable again. Mirrors the ``agent_commands`` / ``staging_transfers`` partial-
index discipline already in the schema (indexes only over the live subset).

**P10-T8 — ``updated_at`` activity watermark.** The staging TTL-cleanup sweep
must distinguish an actively-progressing partial upload from an abandoned one.
``created_at`` never moves and ``last_range_request_at`` only tracks DOWNLOAD
activity, so neither answers "when did this upload last make progress". A
``updated_at`` column (server-default ``now()``, ORM ``onupdate=now()`` so every
PATCH append that advances the offset bumps it) is the missing last-activity
signal: a row in ``pending``/``uploading`` whose ``updated_at`` is older than
``FILEARR_STAGING_ABANDONED_UPLOAD_SECONDS`` is reclaimed early. Additive with a
server default, so existing rows (none in practice — staging is transient) are
backfilled to ``now()`` at migration time.

Purely additive; no data backfill beyond the column default. Round-trips cleanly
(downgrade drops the column then the index).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2f4b6a8c0e1"
down_revision: str | None = "d1e5b9c3a7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # P10-T6: at most one ACTIVE transfer per item (race backstop).
    op.create_index(
        "uq_staging_transfers_active_item",
        "staging_transfers",
        ["item_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending','uploading','staged')"),
    )
    # P10-T8: last-activity watermark for the abandoned-partial reclaim.
    op.add_column(
        "staging_transfers",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("staging_transfers", "updated_at")
    op.drop_index(
        "uq_staging_transfers_active_item", table_name="staging_transfers"
    )
