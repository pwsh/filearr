"""alert_events dedup partial-UNIQUE index (P8-T5 race-proofing)

Revision ID: f3b8d2a41c5e
Revises: e2f4a6c8b0d1
Create Date: 2026-07-12 15:00:00.000000

The P8-T5 inline matcher and the ops-alert hooks (P8-T9/T10) dedup a match with
an application-level ``NOT EXISTS`` check against undelivered rows sharing
``(rule_id, dedup_key, item_id)``. That is correct under the single-scan-per-
library queueing lock, but two *different* writers (a scan racing the minutely
ops pump, or two library scans) could still insert a duplicate between the check
and the commit. This migration closes that TOCTOU with a **partial UNIQUE
index** over exactly the dedup tuple, restricted to *pending* rows:

    UNIQUE (rule_id, dedup_key, COALESCE(item_id, <nil-uuid>)) WHERE NOT delivered

``item_id`` is nullable (ops alerts carry none), and SQL treats NULLs as
distinct — so the tuple is COALESCEd to an all-zero sentinel UUID, making two
NULL-item ops events for the same rule+dedup_key collide (exactly the intended
"one pending ops alert per group" behaviour). The ``WHERE NOT delivered``
predicate matches the state-derived design: once a group is delivered its rows
no longer block a fresh match, so a later re-occurrence inserts cleanly.

With this index the writers switch to ``INSERT ... ON CONFLICT DO NOTHING``
(``filearr.alerts.pipeline.persist_drafts`` / the ops hooks), making the dedup
race-proof at the database rather than relying on the read-then-write window.

The pre-existing non-unique ``ix_alert_events_pending`` (rule_id, dedup_key)
WHERE NOT delivered is kept: it still serves the pump's group-buffer scan, and a
partial index's uniqueness does not make it a drop-in replacement for that
lookup shape.

Downgrade drops only the new unique index (fully additive migration).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "f3b8d2a41c5e"
down_revision: str | None = "e2f4a6c8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NIL_UUID = "00000000-0000-0000-0000-000000000000"
_INDEX = "uq_alert_events_dedup_pending"


_FK = "fk_alert_events_item_id_items"


def upgrade() -> None:
    # SET NULL -> CASCADE so an item delete drops (not NULL-collapses) its pending
    # file-event alerts, keeping the COALESCE dedup index collision-free.
    op.drop_constraint(_FK, "alert_events", type_="foreignkey")
    op.create_foreign_key(
        _FK, "alert_events", "items", ["item_id"], ["id"], ondelete="CASCADE"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON alert_events "
        f"(rule_id, dedup_key, COALESCE(item_id, '{_NIL_UUID}'::uuid)) "
        "WHERE NOT delivered"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.drop_constraint(_FK, "alert_events", type_="foreignkey")
    op.create_foreign_key(
        _FK, "alert_events", "items", ["item_id"], ["id"], ondelete="SET NULL"
    )
