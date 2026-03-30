"""MeliListing model - Mercado Libre listings."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.order import Order


class MeliListing(Base):
    __tablename__ = "meli_listings"
    __table_args__ = (
        # Partial unique index: only one non-closed listing per (product_id, user_id, variation_asin).
        # Defined via raw SQL in migration 0007 (PostgreSQL partial index with COALESCE).
        # Index name: uq_meli_listings_active_product_variation
        # WHERE status != 'closed' — allows re-publishing after closing a listing.
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # user_id and product_id are logical FKs to the backend DB (no FK constraint)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    meli_item_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True, index=True)
    variation_asin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meli_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    meli_category_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False, index=True
    )  # draft | active | paused | closed
    meli_permalink: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    available_quantity: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    listing_type: Mapped[str] = mapped_column(String(20), default="gold_special", nullable=False)
    paused_by_stock: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    meli_picture_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships (within meli-api DB only)
    orders: Mapped[List["Order"]] = relationship(back_populates="listing", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<MeliListing(id={self.id}, meli_item_id='{self.meli_item_id}', status='{self.status}')>"
