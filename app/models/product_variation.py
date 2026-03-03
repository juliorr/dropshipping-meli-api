"""ProductVariation model (read from backend DB)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backend_database import BackendBase


class ProductVariation(BackendBase):
    __tablename__ = "product_variations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asin: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    dimension_key: Mapped[str] = mapped_column(String(30), nullable=False)
    attributes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)    # {"size_name": "1 Count"}
    display_labels: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True) # {"size_name": "Size"}
    amazon_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    images: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)         # List of image URLs
    is_main: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
