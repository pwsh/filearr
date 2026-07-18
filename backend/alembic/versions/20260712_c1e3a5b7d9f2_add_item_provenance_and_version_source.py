"""add items provenance columns + item_versions.source discriminator (P4-T7/T8)

Revision ID: c1e3a5b7d9f2
Revises: f8b3c1d05a29
Create Date: 2026-07-12 00:00:00.000000

Phase 4 provenance extensions (P4-T7 + P4-T8), combined into ONE additive
revision. Three column families:

  * ``items.source_agent_id`` (UUID, nullable) + ``items.replication_seq``
    (BIGINT, nullable) (P4-T7): current-state provenance columns that stay NULL
    in v1 (local-only scanning). They exist now so a phase-5 distributed-agent
    outbox/replication producer can populate them without a later migration
    ("ship the column now, wire the producer later" — mirrors
    ``libraries.hash_full_max_bytes``).
  * ``items.policy_version`` (TEXT, nullable) (P4-T7): a short stable fingerprint
    of the owning library's scan-relevant config, written at extract time
    (``filearr.provenance.policy_version``). Lets a reader tell which config
    version last extracted an item; NULL until the item is (re)extracted.
  * ``item_versions.source`` (TEXT NOT NULL DEFAULT 'user') (P4-T8): the audit
    discriminator distinguishing a user/API edit (``'user'``) from an
    extractor-sourced write (``'scan'`` / ``'extract:<media_type>'``). The
    NOT-NULL server_default backfills every PRE-EXISTING row to ``'user'`` (they
    were all API/UI edits — this table only recorded manual edits until now); the
    explicit UPDATE is belt-and-braces for any NULL that a non-defaulting insert
    path could have left. ``source='user'`` rows are exempt from the P4-T9
    retention purge.

A database that never extracts again is unaffected: the two agent columns stay
NULL, ``policy_version`` stays NULL until the next extract, and every existing
``item_versions`` row reads ``source='user'``.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1e3a5b7d9f2"
down_revision: str | None = "f8b3c1d05a29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # P4-T7: current-state provenance columns on items (all nullable, v1-inert).
    op.add_column(
        "items",
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("items", sa.Column("replication_seq", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("policy_version", sa.Text(), nullable=True))

    # P4-T8: item_versions.source discriminator. NOT NULL + server_default
    # backfills every pre-existing row to 'user' at ADD COLUMN time; the explicit
    # UPDATE covers any NULL a non-defaulting path could have left behind.
    op.add_column(
        "item_versions",
        sa.Column(
            "source", sa.Text(), nullable=False, server_default=sa.text("'user'")
        ),
    )
    op.execute("UPDATE item_versions SET source = 'user' WHERE source IS NULL")

    # P4-T9: composite index backing the retention purge, which filters
    # ``source != 'user'`` AND ``changed_at < cutoff``. Leading ``source``
    # lets the daily purge skip the exempt 'user' partition cheaply.
    op.create_index(
        "ix_item_versions_source_changed_at",
        "item_versions",
        ["source", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_item_versions_source_changed_at", table_name="item_versions")
    op.drop_column("item_versions", "source")
    op.drop_column("items", "policy_version")
    op.drop_column("items", "replication_seq")
    op.drop_column("items", "source_agent_id")
