"""widen access_token and refresh_token columns for encrypted values

Revision ID: 0005_widen_token_columns
Revises: 0004_meli_picture_ids
Create Date: 2026-03-11
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_widen_tokens"
down_revision = "0004_meli_picture_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "meli_tokens", "access_token", type_=sa.String(1024), existing_type=sa.String(500)
    )
    op.alter_column(
        "meli_tokens", "refresh_token", type_=sa.String(1024), existing_type=sa.String(500)
    )


def downgrade() -> None:
    op.alter_column(
        "meli_tokens", "access_token", type_=sa.String(500), existing_type=sa.String(1024)
    )
    op.alter_column(
        "meli_tokens", "refresh_token", type_=sa.String(500), existing_type=sa.String(1024)
    )
