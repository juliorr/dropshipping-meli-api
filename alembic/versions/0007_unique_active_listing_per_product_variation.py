"""Add unique constraint to prevent duplicate active listings per product+variation.

Revision ID: 0007_unique_active_per_product
Revises: 0006_paused_by_stock
Create Date: 2026-03-28
"""

from alembic import op

revision = "0007_unique_active_per_product"
down_revision = "0006_paused_by_stock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX uq_meli_listings_active_product_variation
        ON meli_listings (user_id, product_id, COALESCE(variation_asin, '__none__'))
        WHERE status != 'closed'
    """)


def downgrade() -> None:
    op.drop_index("uq_meli_listings_active_product_variation", table_name="meli_listings")
