"""Mercado Libre schemas - Pydantic models for ML integration."""

from datetime import datetime
from typing import List, Optional, Dict

from pydantic import BaseModel


class MeliAuthUrlResponse(BaseModel):
    """OAuth authorization URL."""
    auth_url: str


class MeliCallbackRequest(BaseModel):
    """OAuth callback with authorization code."""
    code: str


class MeliTokenStatus(BaseModel):
    """Current ML token status for user."""
    connected: bool
    meli_user_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    token_type: Optional[str] = None


class MeliCategory(BaseModel):
    """ML category."""
    id: str
    name: str


class MeliCategoryListResponse(BaseModel):
    """List of ML categories."""
    categories: List[MeliCategory]
    site_id: str


class MeliCategoryAttributeValue(BaseModel):
    """An allowed value for a category attribute."""
    id: str
    name: str


class MeliCategoryAttribute(BaseModel):
    """A category attribute with its metadata."""
    id: str
    name: str
    value_type: str = "string"  # string, number, list, boolean, etc.
    required: bool = False
    catalog_required: bool = False
    is_optional: bool = False            # True = sugerido/opcional por la categoría (no bloquea publicación)
    is_variation_attribute: bool = False # True = este atributo define variantes (SIZE, COLOR, etc.)
    allow_custom_value: bool = True
    allowed_values: Optional[List[MeliCategoryAttributeValue]] = None
    tooltip: Optional[str] = None
    default_value: Optional[str] = None


class MeliCategoryAttributesResponse(BaseModel):
    """Response with required attributes for a category."""
    category_id: str
    attributes: List[MeliCategoryAttribute]


class MeliPublishAttribute(BaseModel):
    """A single attribute to include when publishing."""
    id: str
    value_id: Optional[str] = None   # Use when selecting from allowed_values
    value_name: Optional[str] = None  # Use for free-text values


class VariationPublishItem(BaseModel):
    """A single product variant to include when publishing with variations."""
    asin: str
    attributes: Dict[str, str]      # e.g. {"size_name": "1 Count", "color_name": "Red"}
    display_labels: Dict[str, str]  # e.g. {"size_name": "Size", "color_name": "Color"}
    price: float
    available_quantity: int = 5
    pictures: List[str] = []        # Image URLs for this variant


class MeliPublishRequest(BaseModel):
    """Request to publish a listing to ML."""
    listing_id: int
    # Product data (sent by frontend — meli-api is autonomous, no backend DB access)
    product_id: Optional[int] = None          # For image lookup on local filesystem
    product_brand: Optional[str] = None       # Brand from Amazon product
    product_asin: Optional[str] = None        # ASIN for GTIN injection
    product_title: Optional[str] = None       # Title fallback for model field
    brand: Optional[str] = None
    model: Optional[str] = None
    family_name: Optional[str] = None  # Required by some generic ML categories (e.g. "Otros")
    attributes: Optional[List[MeliPublishAttribute]] = None  # Dynamic attributes for the category
    condition: Optional[str] = None  # "new" | "used" | "refurbished"
    warranty_type: Optional[str] = None  # "Garantía del vendedor" | "Garantía de fábrica" | "Sin garantía"
    warranty_time: Optional[str] = None  # "90 días", "6 meses", "1 año", etc.
    shipping_mode: Optional[str] = None  # "me2" | "me1" | "not_specified" | "custom"
    free_shipping: Optional[bool] = None
    variations: Optional[List[VariationPublishItem]] = None  # Per-variant prices/quantities/images


class MeliPublishResponse(BaseModel):
    """Response after publishing to ML."""
    meli_item_id: str
    permalink: str
    status: str
    product_status: Optional[str] = None         # Suggested product status for frontend to update
    warnings: Optional[List[str]] = None         # Non-blocking warnings from ML
    variations_count: Optional[int] = None       # How many variations were actually published
    variations_dropped: Optional[bool] = None    # True if variations were provided but ignored


class VariantPublishPayload(BaseModel):
    """A single variant to publish as a separate ML item."""
    asin: str
    attributes: Dict[str, str]      # e.g. {"size_name": "1 Count", "color_name": "Red"}
    display_labels: Dict[str, str]
    price: float
    available_quantity: int = 5
    images: List[str] = []
    ml_attributes: Optional[List[MeliPublishAttribute]] = None  # Per-variant ML attributes (COLOR, SIZE, FLAVOR…)


class MeliPublishVariantsRequest(BaseModel):
    """Request to publish multiple variants as separate ML items."""
    base_listing_id: int                       # Draft listing with category/description/price
    variations: List[VariantPublishPayload]
    # Product data (sent by frontend — meli-api is autonomous)
    product_id: Optional[int] = None          # For image lookup on local filesystem
    product_brand: Optional[str] = None       # Brand from Amazon product
    product_asin: Optional[str] = None        # ASIN for GTIN injection
    brand: Optional[str] = None
    family_name: Optional[str] = None          # Required by some generic ML categories
    condition: Optional[str] = "new"
    warranty_type: Optional[str] = None
    warranty_time: Optional[str] = None
    shipping_mode: Optional[str] = None
    free_shipping: Optional[bool] = None
    attributes: Optional[List[MeliPublishAttribute]] = None
