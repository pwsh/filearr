"""add agents.capabilities (W6-D3)

Revision ID: c7d9e1f3a5b8
Revises: f5c8a2b4d6e0
Create Date: 2026-07-18 18:00:00.000000

Wave 6, W6-D3 (extensible inventory framework — capability advertisement).

The distributed agent attaches an additive ``capabilities`` object to every
command poll — ``{inventory_collectors: [...], inventory_version: N}`` — so
central can persist what each agent supports and the UI can offer only the
inventory collectors an agent actually has (a new inventory COMPOSITION then
needs no agent redeploy, just a capable agent).

Two additive changes:

  * ``agents.capabilities`` JSONB NULL (NULL = the agent has not yet polled with a
    capabilities body, or is an older build). Stored verbatim on the poll path
    (:func:`filearr.api.agent_commands.poll_commands`).
  * the ``agent_commands_kind_valid`` CHECK gains ``'inventory'`` so the new
    inventory command kind rides the EXISTING command-creation/queue surface
    (no new enqueue endpoint). Recreated (drop + add) since Postgres CHECKs are
    not alterable in place.

The downgrade drops the column and restores the pre-W6-D3 kind CHECK.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7d9e1f3a5b8"
down_revision: str | None = "f5c8a2b4d6e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KIND_OLD = "kind IN ('stat_check','rehash_check','stage_upload')"
_KIND_NEW = "kind IN ('stat_check','rehash_check','stage_upload','inventory')"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_NEW)


def downgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_OLD)
    op.drop_column("agents", "capabilities")
