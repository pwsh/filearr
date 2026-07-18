"""add libraries.ocr_enabled + expose_gps (P3-T6 OCR opt-in, P3-T11 GPS gate)

Revision ID: b5e8d3a1c7f4
Revises: f3b8d2a41c5e
Create Date: 2026-07-12 00:00:00.000000

Two per-library opt-in booleans, mirroring the existing ``watch_mode`` /
``hash_policy`` per-library override pattern:

  * ``ocr_enabled`` (bool, NOT NULL, default false) — P3-T6 / R4. Global default
    is OFF (``FILEARR_OCR_ENABLED=false``); a library opts IN to the (CPU-costly)
    Tesseract OCR pass. Default-off = zero OCR cost for every existing library.
  * ``expose_gps`` (bool, NOT NULL, default false) — P3-T11 / R5, CWE-1230. GPS
    location fields extracted by the exiftool pass land in ``metadata_`` (extracted
    truth, invariant 2) but are stripped from the Meili projection and the public
    API unless a library explicitly opts IN. Ships in the SAME revision as GPS
    extraction (R5 — never GPS extraction first and the gate as a follow-up).

Both are additive, non-destructive: existing rows get ``false`` (server default),
preserving prior behaviour (no OCR, GPS never exposed).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5e8d3a1c7f4"
down_revision: str | None = "f3b8d2a41c5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "ocr_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "libraries",
        sa.Column(
            "expose_gps",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("libraries", "expose_gps")
    op.drop_column("libraries", "ocr_enabled")
