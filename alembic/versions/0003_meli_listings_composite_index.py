"""add composite index on meli_listings(user_id, status, created_at DESC) and drop redundant single indexes

Revision ID: 0003_meli_listings_composite_index
Revises: 0002_add_variation_asin_to_listings
Create Date: 2026-03-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_composite_index"
down_revision = "0002_variation_asin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_meli_listings_user_status_date",
        "meli_listings",
        ["user_id", "status", sa.text("created_at DESC")],
    )
    op.drop_index("ix_meli_listings_user_id", table_name="meli_listings")
    op.drop_index("ix_meli_listings_status", table_name="meli_listings")


def downgrade() -> None:
    op.create_index("ix_meli_listings_status", "meli_listings", ["status"])
    op.create_index("ix_meli_listings_user_id", "meli_listings", ["user_id"])
    op.drop_index("ix_meli_listings_user_status_date", table_name="meli_listings")
