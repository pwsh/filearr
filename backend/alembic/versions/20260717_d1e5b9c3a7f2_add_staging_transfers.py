"""add staging_transfers (P10-T4 resumable agent->central staging data plane)

Revision ID: d1e5b9c3a7f2
Revises: c4f2a6b8d1e3
Create Date: 2026-07-17 19:30:00.000000

Phase 10, P10-T4. One row per in-flight agent->central retrieve upload, the
durable resume anchor for the hand-rolled offset-``PATCH`` staging protocol
(docs/research/phase-10-t4-transport-spike.md). A row is created (or re-attached)
when an agent picks up a ``stage_upload`` command and initiates the upload; the
agent streams the file body in chunks, and central advances ``bytes_transferred``
only after each chunk is durably written (fsync) — so a restarted agent resumes
from exactly ``bytes_transferred`` (central is the single source of truth for the
resume point).

Columns per the task-doc DDL (docs/tasks/phase-10-agent-transfer-tasks.md
"P10-T4 — staging_transfers"):

  * ``item_id`` / ``agent_id`` / ``command_id`` — FKs (all ``ON DELETE CASCADE``)
    to the item being retrieved, the hosting agent, and the ``stage_upload``
    command that triggered it. ``item_id`` / ``agent_id`` are derived from the
    command server-side (never agent-supplied), so an agent cannot forge a
    transfer for an item/agent it does not own.
  * ``state`` — the ``transfers.TransferState`` lifecycle
    (``pending``/``uploading``/``staged``/``downloaded``/``expired``/``failed``),
    advanced ONLY through ``transfers.transfer_state_machine``.
  * ``bytes_transferred`` / ``total_bytes`` — the committed offset (the resume
    point) and the agent-declared file size. A chunk that would push past
    ``total_bytes`` is refused.
  * ``staged_path`` — the on-disk staged file, named ``<transfer_uuid>.bin``
    (``transfers.staging_path_for`` — the id is the ONLY attacker-influenced
    component and is UUID-validated, so it is traversal-proof by construction)
    under ``FILEARR_STAGING_DIR`` (default ``{config_dir}/staging`` — writable
    central disk, NOT a media mount, R5; invariant 6 untouched).
  * ``verified_hash`` / ``verified`` — the P10-T5 integrity seam. This task
    leaves ``verified=false`` on completion; P10-T5 folds the streaming hash and
    flips it. No download is served on an unverified row.
  * ``expires_at`` — TTL anchor (``FILEARR_STAGING_TRANSFER_TTL_SECONDS``,
    default 24h). Actual cleanup is P10-T8; this task only stamps it.
  * ``last_range_request_at`` — the download-watermark P10-T8 checks so a slow
    active download is not reaped; unused by the upload plane here.

DELIBERATE DDL DEVIATIONS from the task-doc sketch (P10-T1 set the precedent of
documenting these in the migration):

  * **``UNIQUE (command_id)``** (``uq_staging_transfers_command``) — ADDED. The
    task requires transfer creation to be *idempotent per command_id* (a restarted
    agent, or the at-least-once command redelivery, must re-attach the SAME row
    rather than spawn a duplicate). The unique constraint is the mechanism: attach
    is an INSERT that, on conflict, returns the existing row.
  * **``CHECK (state IN (...))``** (``staging_transfers_state_valid``) — ADDED,
    mirroring ``agent_commands``'s status CHECK: the six ``TransferState`` values,
    enforced at the DB so a bad transition can never persist an unknown state.
  * **``CHECK (bytes_transferred >= 0)``** and
    **``CHECK (total_bytes IS NULL OR total_bytes >= 0)``** — ADDED; a negative
    offset/size is nonsensical and cheap to forbid.
  * Index ``ix_staging_transfers_expires`` on ``expires_at`` for non-terminal
    rows — ADDED as the P10-T8 sweep target (same shape as the agent_commands
    sweep index), so the future cleanup task scans only live rows.

Purely additive; no backfill.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1e5b9c3a7f2"
down_revision: str | None = "c4f2a6b8d1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staging_transfers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_commands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "bytes_transferred",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("staged_path", sa.Text(), nullable=True),
        sa.Column("verified_hash", sa.Text(), nullable=True),
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_range_request_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('pending','uploading','staged','downloaded','expired','failed')",
            name="staging_transfers_state_valid",
        ),
        sa.CheckConstraint(
            "bytes_transferred >= 0", name="staging_transfers_bytes_nonneg"
        ),
        sa.CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="staging_transfers_total_nonneg",
        ),
        # Idempotent attach: exactly one transfer per stage_upload command.
        sa.UniqueConstraint("command_id", name="uq_staging_transfers_command"),
    )
    # P10-T8 sweep target: only ever scans non-terminal rows.
    op.create_index(
        "ix_staging_transfers_expires",
        "staging_transfers",
        ["expires_at"],
        postgresql_where=sa.text("state IN ('pending','uploading','staged')"),
    )
    # Route from an item to its in-flight/staged transfers (UI + P10-T6).
    op.create_index("ix_staging_transfers_item", "staging_transfers", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_staging_transfers_item", table_name="staging_transfers")
    op.drop_index("ix_staging_transfers_expires", table_name="staging_transfers")
    op.drop_table("staging_transfers")
