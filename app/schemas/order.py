"""Order schemas - Pydantic models for orders."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class OrderResponse(BaseModel):
    id: int
    user_id: int
    listing_id: int
    meli_order_id: Optional[str] = None
    buyer_nickname: Optional[str] = None
    quantity: int
    unit_price: float
    total_amount: float
    status: str
    order_date: Optional[datetime] = None
    shipping_status: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderListResponse(BaseModel):
    """Paginated order list."""
    items: List[OrderResponse]
    total: int
    page: int
    page_size: int
    pages: int


class OrderSyncResponse(BaseModel):
    """Response for order sync endpoint."""
    new_orders: int
    updated_orders: int
    total_fetched: int = 0
    error: Optional[str] = None
    errors: Optional[List[str]] = None
