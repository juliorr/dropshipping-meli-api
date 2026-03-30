"""add paused_by_stock flag to meli_listings

Revision ID: 0006_paused_by_stock
Revises: 0005_widen_tokens
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_paused_by_stock"
down_revision = "0005_widen_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meli_listings",
        sa.Column("paused_by_stock", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("meli_listings", "paused_by_stock")
