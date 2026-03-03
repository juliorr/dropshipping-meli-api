"""Listings router - CRUD endpoints for ML listing drafts."""

import math
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db, verify_api_key
from app.models.listing import MeliListing
from app.schemas.listing import (
    ListingCreate,
    ListingListResponse,
    ListingResponse,
    ListingUpdate,
)
from app.services.meli_categories import is_catalog_only_category

router = APIRouter(prefix="/listings", tags=["Listings"])


@router.get("", response_model=ListingListResponse)
async def list_listings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    search: Optional[str] = Query(None, description="Search in title"),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List ML listings for the current user."""
    query = select(MeliListing).where(MeliListing.user_id == current_user.id)
    if status_filter:
        query = query.where(MeliListing.status == status_filter)
    if search:
        query = query.where(MeliListing.title.ilike(f"%{search}%"))

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(MeliListing.created_at.desc()).offset(offset).limit(page_size)
    items = (await db.execute(query)).scalars().all()

    return {
        "items": items, "total": total, "page": page,
        "page_size": page_size, "pages": math.ceil(total / page_size) if total > 0 else 0,
    }


@router.post("", response_model=ListingResponse, status_code=status.HTTP_201_CREATED)
async def create_listing(
    data: ListingCreate,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new ML listing draft."""
    if data.meli_category_id:
        if await is_catalog_only_category(data.meli_category_id):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "catalog_required_category",
                    "message": (
                        f"La categoría '{data.meli_category_id}' requiere modo catálogo "
                        "y no está disponible para publicaciones personalizadas. "
                        "Por favor selecciona una categoría diferente."
                    ),
                },
            )

    # Deduplicate draft by variation_asin scope
    if data.variation_asin:
        draft_query = select(MeliListing).where(
            MeliListing.product_id == data.product_id,
            MeliListing.user_id == current_user.id,
            MeliListing.variation_asin == data.variation_asin,
            MeliListing.meli_item_id.is_(None),
        )
    else:
        draft_query = select(MeliListing).where(
            MeliListing.product_id == data.product_id,
            MeliListing.user_id == current_user.id,
            MeliListing.variation_asin.is_(None),
            MeliListing.meli_item_id.is_(None),
        )
    existing_draft = (await db.execute(draft_query)).scalar_one_or_none()

    if existing_draft:
        existing_draft.title = data.title
        existing_draft.description = data.description
        existing_draft.meli_price = data.meli_price
        existing_draft.available_quantity = data.available_quantity
        existing_draft.meli_category_id = data.meli_category_id
        existing_draft.listing_type = data.listing_type
        existing_draft.status = "draft"
        await db.commit()
        await db.refresh(existing_draft)
        return existing_draft

    listing = MeliListing(
        user_id=current_user.id,
        product_id=data.product_id,
        title=data.title,
        description=data.description,
        meli_price=data.meli_price,
        available_quantity=data.available_quantity,
        meli_category_id=data.meli_category_id,
        listing_type=data.listing_type,
        variation_asin=data.variation_asin,
        status="draft",
    )
    db.add(listing)
    await db.commit()
    await db.refresh(listing)
    return listing


@router.get("/by-product/{product_id}", response_model=List[ListingResponse])
async def get_listings_by_product(
    product_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all listings for a product (returns [] if none — no 404)."""
    listings = (await db.execute(
        select(MeliListing)
        .where(MeliListing.product_id == product_id, MeliListing.user_id == current_user.id)
        .order_by(MeliListing.created_at.asc())
    )).scalars().all()
    return listings


@router.get("/{listing_id}", response_model=ListingResponse)
async def get_listing(
    listing_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get listing details."""
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


@router.put("/{listing_id}", response_model=ListingResponse)
async def update_listing(
    listing_id: int,
    data: ListingUpdate,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a listing."""
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    update_data = data.model_dump(exclude_unset=True)
    new_category_id = update_data.get("meli_category_id")
    if new_category_id and new_category_id != listing.meli_category_id:
        if await is_catalog_only_category(new_category_id):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "catalog_required_category",
                    "message": (
                        f"La categoría '{new_category_id}' requiere modo catálogo "
                        "y no está disponible para publicaciones personalizadas. "
                        "Por favor selecciona una categoría diferente."
                    ),
                },
            )

    for field, value in update_data.items():
        setattr(listing, field, value)

    await db.commit()
    await db.refresh(listing)
    return listing


@router.delete("/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_listing(
    listing_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete/close a listing."""
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    await db.delete(listing)
    await db.commit()


@router.get("/stats")
async def get_listing_stats(
    user_id: int = Query(..., description="User ID"),
    _: str = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Internal endpoint — ML stats for the dashboard."""
    from sqlalchemy import func as sqlfunc
    from app.models.order import Order

    active_listings = (await db.execute(
        select(sqlfunc.count()).where(MeliListing.user_id == user_id, MeliListing.status == "active")
    )).scalar() or 0

    total_orders = (await db.execute(
        select(sqlfunc.count()).where(Order.user_id == user_id)
    )).scalar() or 0

    pending_orders = (await db.execute(
        select(sqlfunc.count()).where(Order.user_id == user_id, Order.status == "pending")
    )).scalar() or 0

    total_revenue = (await db.execute(
        select(sqlfunc.coalesce(sqlfunc.sum(Order.total_amount), 0)).where(Order.user_id == user_id)
    )).scalar() or 0.0

    return {
        "active_listings": active_listings,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "total_revenue": float(total_revenue),
    }


@router.get("/by-products/batch", response_model=Dict[int, List[dict]])
async def get_listings_by_product_ids(
    product_ids: str = Query(..., description="Comma-separated list of product IDs"),
    user_id: int = Query(..., description="User ID"),
    _: str = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Internal endpoint (API key protected) — batch fetch listings by product IDs.
    Used by backend-api to enrich the products list without cross-DB queries.
    Returns a map of product_id → List[{has_listing, meli_status, meli_item_id, variation_asin}].
    """
    ids: List[int] = [int(x) for x in product_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return {}

    result = await db.execute(
        select(MeliListing).where(
            MeliListing.product_id.in_(ids),
            MeliListing.user_id == user_id,
        ).order_by(MeliListing.created_at.asc())
    )
    listings = result.scalars().all()

    out: Dict[int, List[dict]] = {}
    for listing in listings:
        out.setdefault(listing.product_id, []).append({
            "has_listing": True,
            "listing_id": listing.id,
            "meli_status": listing.status,
            "meli_item_id": listing.meli_item_id,
            "variation_asin": listing.variation_asin,
        })
    return out
