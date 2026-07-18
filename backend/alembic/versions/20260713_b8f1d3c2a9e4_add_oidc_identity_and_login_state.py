"""P6-T5 OIDC SSO: user external-identity linking + login-state table

Revision ID: b8f1d3c2a9e4
Revises: a4c8e1f6b2d9
Create Date: 2026-07-13 16:30:00.000000

Additive, zero-behaviour-change migration for OIDC/SSO (P6-T5). Nothing here
engages until ``FILEARR_OIDC_ENABLED=true``; existing local accounts are
untouched.

1. ``users.external_issuer`` (nullable TEXT) — the IdP that asserted the subject.
   An OIDC ``sub`` is unique only WITHIN its issuer, so the stable federated
   identity is the triple (auth_provider, external_issuer, external_subject).
   ``external_subject`` already exists from the P6-T1 identity foundation; this
   adds the issuer half.

2. ``uq_users_external_identity`` — a PARTIAL unique index on
   (auth_provider, external_issuer, external_subject) WHERE external_subject IS
   NOT NULL. Partial so the many local rows (all-NULL subject) never collide,
   while a given IdP subject can back at most one Filearr account (JIT provision
   / link idempotency).

3. ``oidc_login_states`` — server-side single-use holder for the CSRF ``state``,
   the ID-token ``nonce``, and the PKCE ``code_verifier`` across the redirect to
   the IdP and back (a SameSite=Lax cookie cannot carry these on the cross-site
   callback). Rows are consumed-and-deleted at the callback and TTL-expired
   otherwise.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8f1d3c2a9e4"
down_revision: str | None = "a4c8e1f6b2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("external_issuer", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_users_external_identity",
        "users",
        ["auth_provider", "external_issuer", "external_subject"],
        unique=True,
        postgresql_where=sa.text("external_subject IS NOT NULL"),
    )
    op.create_table(
        "oidc_login_states",
        sa.Column("state", sa.Text(), primary_key=True),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), server_default=sa.text("'/'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_oidc_login_states_created", "oidc_login_states", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_oidc_login_states_created", table_name="oidc_login_states")
    op.drop_table("oidc_login_states")
    op.drop_index("uq_users_external_identity", table_name="users")
    op.drop_column("users", "external_issuer")
