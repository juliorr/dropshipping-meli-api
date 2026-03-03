"""Initial meli-api tables: meli_tokens, meli_listings, orders.

Revision ID: 0001_initial_meli_tables
Revises:
Create Date: 2026-02-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_meli_tables"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meli_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("access_token", sa.String(500), nullable=False),
        sa.Column("refresh_token", sa.String(500), nullable=False),
        sa.Column("token_type", sa.String(20), nullable=False, server_default="Bearer"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meli_user_id", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_meli_tokens_user_id", "meli_tokens", ["user_id"])

    op.create_table(
        "meli_listings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("meli_item_id", sa.String(50), nullable=True),
        sa.Column("title", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("meli_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("meli_category_id", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("meli_permalink", sa.String(500), nullable=True),
        sa.Column("available_quantity", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("listing_type", sa.String(20), nullable=False, server_default="gold_special"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("meli_item_id"),
    )
    op.create_index("ix_meli_listings_user_id", "meli_listings", ["user_id"])
    op.create_index("ix_meli_listings_product_id", "meli_listings", ["product_id"])
    op.create_index("ix_meli_listings_meli_item_id", "meli_listings", ["meli_item_id"])
    op.create_index("ix_meli_listings_status", "meli_listings", ["status"])

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("meli_order_id", sa.String(50), nullable=True),
        sa.Column("buyer_nickname", sa.String(255), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("order_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shipping_status", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["meli_listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("meli_order_id"),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_listing_id", "orders", ["listing_id"])
    op.create_index("ix_orders_meli_order_id", "orders", ["meli_order_id"])
    op.create_index("ix_orders_status", "orders", ["status"])


def downgrade() -> None:
    op.drop_table("orders")
    op.drop_table("meli_listings")
    op.drop_table("meli_tokens")
