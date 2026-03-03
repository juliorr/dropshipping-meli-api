"""Listing schemas - Pydantic models for Mercado Libre listings."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ListingBase(BaseModel):
    title: str = Field(..., max_length=60, description="ML title limit: 60 chars")
    description: Optional[str] = None
    meli_price: float = Field(..., gt=0)
    available_quantity: int = Field(15, ge=0, description="Stock quantity for ML listing")
    meli_category_id: Optional[str] = None
    listing_type: str = "gold_special"  # gold_special | gold_pro


class ListingCreate(ListingBase):
    """Create a listing from a product."""
    product_id: int
    variation_asin: Optional[str] = None


class ListingUpdate(BaseModel):
    """Update listing fields (partial)."""
    title: Optional[str] = Field(None, max_length=60)
    description: Optional[str] = None
    meli_price: Optional[float] = Field(None, gt=0)
    available_quantity: Optional[int] = Field(None, ge=0)
    meli_category_id: Optional[str] = None
    status: Optional[str] = None  # draft | active | paused | closed
    listing_type: Optional[str] = None


class ListingResponse(ListingBase):
    id: int
    user_id: int
    product_id: int
    meli_item_id: Optional[str] = None
    variation_asin: Optional[str] = None
    available_quantity: int = 15
    status: str
    meli_permalink: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ListingListResponse(BaseModel):
    """Paginated listing list."""
    items: List[ListingResponse]
    total: int
    page: int
    page_size: int
    pages: int


class ListingVariantResult(BaseModel):
    variation_asin: str
    variant_name: Optional[str] = None   # Human-readable label (e.g. "Cacao Nib Crunch")
    success: bool
    listing_id: Optional[int] = None
    meli_item_id: Optional[str] = None
    permalink: Optional[str] = None
    error: Optional[str] = None


class MeliPublishBulkResponse(BaseModel):
    results: List[ListingVariantResult]
    total: int
    succeeded: int
    failed: int
