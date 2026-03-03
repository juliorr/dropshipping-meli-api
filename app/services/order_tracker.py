"""Order tracker service - Order management and queries."""

import logging
import math
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order

logger = logging.getLogger(__name__)


async def get_orders(
    db: AsyncSession,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
) -> dict:
    """Get paginated orders for a user."""
    query = select(Order).where(Order.user_id == user_id)

    if status:
        query = query.where(Order.status == status)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    query = query.order_by(Order.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(query)
    items = result.scalars().all()

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total > 0 else 0,
    }


async def get_order_by_id(
    db: AsyncSession, order_id: int, user_id: int
) -> Optional[Order]:
    """Get a single order by ID."""
    query = select(Order).where(Order.id == order_id, Order.user_id == user_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def sync_orders_for_user(db: AsyncSession, user_id: int) -> dict:
    """
    Sync orders from Mercado Libre for a specific user.
    Fetches orders from the ML API and creates/updates them in the database.
    Returns a summary dict with counts.
    """
    from app.models.meli_token import MeliToken
    from app.models.listing import MeliListing
    from app.services.meli_client import get_orders as meli_get_orders

    # Get the user's ML token
    result = await db.execute(
        select(MeliToken).where(MeliToken.user_id == user_id)
    )
    token = result.scalar_one_or_none()

    if not token:
        return {"error": "No hay cuenta de Mercado Libre conectada", "new_orders": 0, "updated_orders": 0}

    if not token.meli_user_id:
        return {"error": "No se encontró el ID de usuario de ML", "new_orders": 0, "updated_orders": 0}

    new_orders = 0
    updated_orders = 0
    errors = []

    try:
        # Fetch orders from ML API (get recent orders, multiple pages)
        offset = 0
        limit = 50
        total_fetched = 0

        while True:
            orders_data = await meli_get_orders(
                db, user_id, token.meli_user_id, offset=offset, limit=limit
            )

            if not orders_data or orders_data.get("error"):
                error_detail = orders_data.get("detail", "Error desconocido") if orders_data else "Sin respuesta de ML"
                errors.append(f"Error al obtener órdenes (offset={offset}): {error_detail}")
                break

            results = orders_data.get("results", [])
            if not results:
                break

            for order_data in results:
                meli_order_id = str(order_data.get("id"))

                # Check if order already exists
                existing = (await db.execute(
                    select(Order).where(Order.meli_order_id == meli_order_id)
                )).scalar_one_or_none()

                if existing:
                    # Update status if changed
                    new_status = order_data.get("status", existing.status)
                    new_shipping = order_data.get("shipping", {}).get("status")
                    if existing.status != new_status or existing.shipping_status != new_shipping:
                        existing.status = new_status
                        existing.shipping_status = new_shipping
                        updated_orders += 1
                    continue

                # Find matching listing
                items = order_data.get("order_items", [])
                if not items:
                    continue

                meli_item_id = items[0].get("item", {}).get("id")
                listing = (await db.execute(
                    select(MeliListing).where(MeliListing.meli_item_id == meli_item_id)
                )).scalar_one_or_none()

                if not listing:
                    logger.warning(f"No listing found for ML item {meli_item_id}, skipping order {meli_order_id}")
                    continue

                new_order = Order(
                    user_id=user_id,
                    listing_id=listing.id,
                    meli_order_id=meli_order_id,
                    buyer_nickname=order_data.get("buyer", {}).get("nickname"),
                    quantity=items[0].get("quantity", 1),
                    unit_price=items[0].get("unit_price", 0),
                    total_amount=order_data.get("total_amount", 0),
                    status=order_data.get("status", "pending"),
                    shipping_status=order_data.get("shipping", {}).get("status"),
                )
                db.add(new_order)
                new_orders += 1

            total_fetched += len(results)
            total_available = orders_data.get("paging", {}).get("total", 0)

            # Stop if we've fetched all or reached a reasonable limit
            if total_fetched >= total_available or total_fetched >= 200:
                break

            offset += limit

        await db.commit()

    except Exception as e:
        logger.error(f"Order sync failed for user {user_id}: {e}")
        errors.append(str(e))

    result = {
        "new_orders": new_orders,
        "updated_orders": updated_orders,
        "total_fetched": total_fetched if 'total_fetched' in dir() else 0,
    }
    if errors:
        result["errors"] = errors

    logger.info(f"Order sync for user {user_id}: {result}")
    return result
