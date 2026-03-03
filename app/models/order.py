"""Order model - Mercado Libre sales orders."""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.listing import MeliListing


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # user_id is a logical FK to the backend's users table (no FK constraint across DBs)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("meli_listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meli_order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True, index=True)
    buyer_nickname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )  # pending | paid | shipped | delivered | cancelled
    order_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    shipping_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    listing: Mapped["MeliListing"] = relationship(back_populates="orders")

    def __repr__(self) -> str:
        return f"<Order(id={self.id}, meli_order_id='{self.meli_order_id}', status='{self.status}')>"
