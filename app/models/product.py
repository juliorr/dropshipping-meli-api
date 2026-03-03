"""
Product model (read from backend DB).

This model maps to the `products` table in the backend's PostgreSQL.
The meli-api reads product data and updates product.status when publishing.
This coupling will be removed in a future phase (status updates via HTTP).
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.backend_database import BackendBase
from app.models.product_image import ProductImage


class Product(BackendBase):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    asin: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amazon_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    amazon_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    rating: Mapped[Optional[float]] = mapped_column(Numeric(3, 1), nullable=True)
    reviews_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    availability: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    available_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    features: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    is_smoke_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="scraped", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    images: Mapped[List["ProductImage"]] = relationship(
        "ProductImage", primaryjoin="Product.id == foreign(ProductImage.product_id)", lazy="select"
    )
