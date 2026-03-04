"""add meli_picture_ids JSON column to meli_listings

Revision ID: 0004_add_meli_picture_ids
Revises: 0003_meli_listings_composite_index
Create Date: 2026-03-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_meli_picture_ids"
down_revision = "0003_composite_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meli_listings",
        sa.Column("meli_picture_ids", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meli_listings", "meli_picture_ids")
