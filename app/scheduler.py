"""Background scheduler for periodic tasks.

Replaces Celery Beat + Worker with APScheduler running inside the FastAPI process.
All tasks are lightweight HTTP calls + DB queries — no heavy computation.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import async_session

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/Mexico_City")


async def _get_session():
    async with async_session() as session:
        yield session


async def sync_meli_listings():
    """Sync ML listing statuses every 2 hours."""
    logger.info("[Scheduler] Starting ML listings sync...")
    from sqlalchemy import select
    from app.models.listing import MeliListing
    from app.services.meli_client import get_item

    synced = 0
    errors = 0

    async with async_session() as db:
        result = await db.execute(
            select(MeliListing).where(
                MeliListing.meli_item_id.isnot(None),
                MeliListing.status.in_(["active", "paused"]),
            ).limit(100)
        )
        listings = result.scalars().all()

        for listing in listings:
            try:
                item_data = await get_item(db, listing.user_id, listing.meli_item_id)
                if item_data and not item_data.get("error"):
                    ml_status = item_data.get("status", "")
                    if ml_status and ml_status != listing.status:
                        listing.status = ml_status
                        synced += 1
                elif item_data and item_data.get("status") == 404:
                    listing.status = "closed"
                    synced += 1
            except Exception as e:
                errors += 1
                logger.error(f"[Scheduler] Sync failed for {listing.meli_item_id}: {e}")

        await db.commit()

    logger.info(f"[Scheduler] Listings sync: checked={len(listings)}, synced={synced}, errors={errors}")


async def sync_meli_orders():
    """Fetch new orders from ML every hour."""
    logger.info("[Scheduler] Starting ML orders sync...")
    from sqlalchemy import select
    from app.models.meli_token import MeliToken
    from app.models.order import Order
    from app.models.listing import MeliListing
    from app.services.meli_client import get_orders

    new_orders = 0

    async with async_session() as db:
        result = await db.execute(select(MeliToken))
        tokens = result.scalars().all()

        for token in tokens:
            try:
                orders_data = await get_orders(db, token.user_id, token.meli_user_id)
                if not orders_data or orders_data.get("error"):
                    continue

                for order_data in orders_data.get("results", []):
                    meli_order_id = str(order_data.get("id"))

                    existing = (await db.execute(
                        select(Order).where(Order.meli_order_id == meli_order_id)
                    )).scalar_one_or_none()

                    if existing:
                        existing.status = order_data.get("status", existing.status)
                        existing.shipping_status = order_data.get("shipping", {}).get("status")
                        continue

                    items = order_data.get("order_items", [])
                    if not items:
                        continue

                    meli_item_id = items[0].get("item", {}).get("id")
                    listing = (await db.execute(
                        select(MeliListing).where(MeliListing.meli_item_id == meli_item_id)
                    )).scalar_one_or_none()

                    if not listing:
                        continue

                    new_order = Order(
                        user_id=token.user_id,
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

            except Exception as e:
                logger.error(f"[Scheduler] Order sync failed for user {token.user_id}: {e}")

        await db.commit()

    logger.info(f"[Scheduler] Orders sync: new_orders={new_orders}")


async def sync_meli_categories():
    """Sync MeLi category tree to in-memory cache. Weekly."""
    logger.info("[Scheduler] Starting MeLi categories sync...")
    from app.services.meli_categories import sync_categories_to_cache

    count = await sync_categories_to_cache("MLM")
    logger.info(f"[Scheduler] Categories sync: {count} categories cached")


async def refresh_meli_tokens():
    """Proactively refresh ML tokens before expiry. Every 5 hours."""
    logger.info("[Scheduler] Starting ML token refresh...")
    from sqlalchemy import select
    from app.models.meli_token import MeliToken
    from app.services.meli_auth import refresh_meli_token

    refreshed = 0
    errors = 0

    async with async_session() as db:
        result = await db.execute(select(MeliToken))
        tokens = result.scalars().all()

        for token in tokens:
            try:
                new_token = await refresh_meli_token(db, token.user_id)
                if new_token:
                    refreshed += 1
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                logger.error(f"[Scheduler] Token refresh error for user {token.user_id}: {e}")

    logger.info(f"[Scheduler] Token refresh: refreshed={refreshed}, errors={errors}")


async def update_meli_price(listing_id: int, new_price: float) -> dict:
    """Update the price of a specific ML listing. Called on-demand."""
    logger.info(f"[Task] Updating ML price for listing {listing_id} to ${new_price}")
    from sqlalchemy import select
    from app.models.listing import MeliListing
    from app.services.meli_client import update_price

    async with async_session() as db:
        listing = (await db.execute(
            select(MeliListing).where(MeliListing.id == listing_id)
        )).scalar_one_or_none()

        if not listing or not listing.meli_item_id:
            return {"error": "Listing not found or not published"}

        result = await update_price(db, listing.user_id, listing.meli_item_id, new_price)
        if result and not result.get("error"):
            listing.meli_price = new_price
            await db.commit()
            return {"status": "updated", "listing_id": listing_id, "new_price": new_price}

        return {"status": "failed", "detail": str(result)}


def setup_scheduler():
    """Register all periodic tasks."""
    scheduler.add_job(
        sync_meli_listings,
        CronTrigger(minute=30, hour="*/2"),
        id="sync-meli-listings",
        name="Sync ML listing statuses",
        replace_existing=True,
    )
    scheduler.add_job(
        sync_meli_orders,
        CronTrigger(minute=0, hour="*/1"),
        id="sync-meli-orders",
        name="Sync ML orders",
        replace_existing=True,
    )
    scheduler.add_job(
        sync_meli_categories,
        CronTrigger(minute=0, hour=3, day_of_week="mon"),
        id="sync-meli-categories",
        name="Sync ML categories weekly",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_meli_tokens,
        CronTrigger(minute=0, hour="*/5"),
        id="refresh-meli-tokens",
        name="Refresh ML tokens",
        replace_existing=True,
    )
    logger.info("[Scheduler] 4 periodic tasks registered")
