"""Orders router - Order endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.schemas.order import OrderListResponse, OrderResponse, OrderSyncResponse
from app.services.order_tracker import get_order_by_id, get_orders, sync_orders_for_user

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.get("", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="Filter by status"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List orders for the current user."""
    result = await get_orders(db, current_user.id, page=page, page_size=page_size, status=status)
    return result


@router.post("/sync", response_model=OrderSyncResponse)
async def sync_orders(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually sync orders from Mercado Libre for the current user."""
    result = await sync_orders_for_user(db, current_user.id)
    return result


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get order details."""
    order = await get_order_by_id(db, order_id, current_user.id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
