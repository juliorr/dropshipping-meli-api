"""add variation_asin to meli_listings

Revision ID: 0002_add_variation_asin_to_listings
Revises: 0001_initial_meli_tables
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_variation_asin"
down_revision = "0001_initial_meli_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("meli_listings", sa.Column("variation_asin", sa.String(20), nullable=True))
    op.create_index(
        "ix_meli_listings_product_variation",
        "meli_listings",
        ["product_id", "user_id", "variation_asin"],
    )


def downgrade():
    op.drop_index("ix_meli_listings_product_variation", table_name="meli_listings")
    op.drop_column("meli_listings", "variation_asin")
