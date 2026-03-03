"""Mercado Libre categories service - Category listing, prediction, and attribute discovery."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.cache import cache
from app.config import settings
from app.schemas.meli import MeliCategory, MeliCategoryAttribute

logger = logging.getLogger(__name__)

MELI_API_BASE = "https://api.mercadolibre.com"


async def get_site_categories() -> List[MeliCategory]:
    """Get top-level categories for the ML site (MLM = Mexico)."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/sites/{settings.meli_site_id}/categories",
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.error(f"Failed to get categories: {response.status_code}")
            return []

        data = response.json()
        return [MeliCategory(id=cat["id"], name=cat["name"]) for cat in data]

    except Exception as e:
        logger.error(f"Error fetching categories: {e}")
        return []


async def get_category_children(category_id: str) -> List[MeliCategory]:
    """
    Get child categories for a given category.
    Automatically filters out catalog-only subcategories (catalog_listing=force).
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/categories/{category_id}",
                timeout=10.0,
            )

        if response.status_code != 200:
            return []

        data = response.json()
        children = data.get("children_categories", [])
        if not children:
            return []

        # Check catalog_listing status for all children concurrently
        catalog_flags = await asyncio.gather(
            *[is_catalog_only_category(cat["id"]) for cat in children]
        )

        result = []
        for cat, is_catalog in zip(children, catalog_flags):
            if not is_catalog:
                result.append(MeliCategory(id=cat["id"], name=cat["name"]))
            else:
                logger.info(f"Filtered out catalog-only child category: {cat['id']} ({cat['name']})")

        return result

    except Exception as e:
        logger.error(f"Error fetching category children: {e}")
        return []


async def search_categories_by_text(query: str, site_id: str = "MLM", limit: int = 10) -> List[MeliCategory]:
    """
    Search ML categories by text using the domain_discovery endpoint.
    Returns a list of matching leaf categories sorted by relevance.
    Filters out catalog-only categories (catalog_listing=force).
    Uses: GET /sites/{site_id}/domain_discovery/search?q={query}&limit={limit}
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/sites/{site_id}/domain_discovery/search",
                params={"q": query, "limit": limit},
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(f"Category text search failed: {response.status_code}")
            return []

        data = response.json()
        seen_ids: set = set()
        candidates: List[MeliCategory] = []
        for item in data:
            cat_id = item.get("category_id")
            cat_name = item.get("category_name") or item.get("domain_name", "")
            if cat_id and cat_id not in seen_ids:
                seen_ids.add(cat_id)
                candidates.append(MeliCategory(id=cat_id, name=cat_name))

        if not candidates:
            return []

        # Filter out catalog-only categories concurrently
        catalog_flags = await asyncio.gather(
            *[is_catalog_only_category(cat.id) for cat in candidates]
        )

        result = []
        for cat, is_catalog in zip(candidates, catalog_flags):
            if not is_catalog:
                result.append(cat)
            else:
                logger.info(f"Filtered out catalog-only category from search: {cat.id} ({cat.name})")

        return result

    except Exception as e:
        logger.error(f"Error searching categories by text: {e}")
        return []


async def get_category_siblings(category_id: str) -> List[MeliCategory]:
    """
    Get sibling categories of a given category (other children of its parent).
    Useful to suggest alternatives when a category is catalog-only.
    Returns an empty list if the category has no parent (is root).
    """
    parent = await get_parent_category(category_id)
    if not parent:
        return []
    return await get_category_children(parent["id"])


async def translate_to_spanish(text: str) -> Optional[str]:
    """
    Translate text to Spanish using Google Translate's public API (no key required).
    Uses sl=auto so it works with any source language; if the text is already in
    Spanish the translation is returned unchanged.

    Returns the translated string, or None if the translation fails.
    Callers should fall back to the original text on None.
    """
    if not text or not text.strip():
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",   # auto-detect source language
                    "tl": "es",     # target: Spanish
                    "dt": "t",
                    "q": text,
                },
                timeout=5.0,
            )
        if resp.status_code == 200:
            data = resp.json()
            # Response structure: [[[translated_segment, original, ...], ...], ...]
            parts = data[0] if data else []
            translated = "".join(seg[0] for seg in parts if seg and seg[0])
            if translated:
                logger.info(f"Translated title: '{text[:60]}' → '{translated[:60]}'")
                return translated.strip()
    except Exception as e:
        logger.warning(f"Translation failed for '{text[:60]}': {e}")
    return None


async def predict_category(title: str) -> Optional[str]:
    """
    Use ML's category predictor to suggest the best category for a product title.
    Translates the title to Spanish first so ML (which operates in Spanish) returns
    better predictions for English product titles scraped from Amazon.
    Returns the first predicted category ID that is NOT catalog-only.
    Skips any catalog-only categories (catalog_listing=force).
    """
    try:
        # Translate to Spanish before querying ML — improves prediction quality
        translated = await translate_to_spanish(title)
        query = translated or title  # fallback to original if translation fails

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/sites/{settings.meli_site_id}/domain_discovery/search",
                params={"q": query},
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(f"Category prediction failed: {response.status_code}")
            return None

        data = response.json()
        if not data:
            return None

        # Extract candidate category IDs (preserving order / relevance)
        candidate_ids = [item.get("category_id") for item in data if item.get("category_id")]
        if not candidate_ids:
            return None

        # Check all candidates concurrently
        catalog_flags = await asyncio.gather(
            *[is_catalog_only_category(cat_id) for cat_id in candidate_ids]
        )

        for cat_id, is_catalog in zip(candidate_ids, catalog_flags):
            if not is_catalog:
                logger.info(f"Predicted category (non-catalog): {cat_id}")
                return cat_id
            else:
                logger.info(f"Skipping catalog-only predicted category: {cat_id}")

        logger.warning(f"All predicted categories for '{title[:50]}' are catalog-only")
        return None

    except Exception as e:
        logger.error(f"Error predicting category: {e}")
        return None


async def search_catalog_product(
    title: str,
    category_id: Optional[str] = None,
    site_id: str = "MLM",
    limit: int = 10,
    access_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search for products in the ML catalog by title (and optionally category).
    Returns a list of catalog products with id, name, and attributes.
    Uses: GET /products/search?site_id=MLM&q={title}&category={category_id}

    NOTE: This endpoint requires a valid Bearer token (access_token).
    """
    try:
        params: Dict[str, Any] = {"site_id": site_id, "q": title, "limit": limit}
        if category_id:
            params["category"] = category_id

        headers: Dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/products/search",
                params=params,
                headers=headers,
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(f"Catalog search failed {response.status_code}: {response.text[:200]}")
            return []

        data = response.json()
        results = data.get("results", [])
        return [
            {
                "catalog_product_id": r.get("id"),
                "name": r.get("name"),
                "category_id": r.get("category_id"),
                "attributes": r.get("attributes", []),
            }
            for r in results
            if r.get("id")
        ]

    except Exception as e:
        logger.error(f"Error searching ML catalog: {e}")
        return []


async def get_parent_category(category_id: str) -> Optional[dict]:
    """
    Get the parent category of a given category.
    Returns a dict with 'id' and 'name', or None if no parent exists.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/categories/{category_id}",
                timeout=10.0,
            )

        if response.status_code != 200:
            return None

        data = response.json()
        path_from_root = data.get("path_from_root", [])
        # The parent is the second-to-last entry in path_from_root (last is the category itself)
        if len(path_from_root) >= 2:
            parent = path_from_root[-2]
            return {"id": parent.get("id"), "name": parent.get("name")}
        elif len(path_from_root) == 1:
            # Already at root, no parent
            return None
        return None

    except Exception as e:
        logger.error(f"Error fetching parent category for {category_id}: {e}")
        return None


async def is_catalog_only_category(category_id: str) -> bool:
    """
    Check if a category is unsuitable for normal (non-catalog) publishing.

    Checks in order (fast to slow):

    1. Redis cache (meli:categories:MLM) — no API calls needed:
       a. settings.catalog_listing == "force" → catalog-only
       b. settings.vertical == "consumer_goods" AND settings.subscribable == True
          → ML requires family_name for these categories (detected from Redis settings)

    2. MeLi API fallback (only if category not in Redis):
       GET /categories/{id} → settings.catalog_listing == "force"
       GET /categories/{id}/attributes → FAMILY/FAMILY_NAME with required tag
    """
    # ── Step 1: Try Redis cache first ──
    cat_data = await _get_category_from_cache(category_id)
    if cat_data:
        settings_data = cat_data.get("settings", {})

        # Signal 1a: consumer_goods + subscribable → ML requires family_name
        vertical = settings_data.get("vertical", "")
        subscribable = settings_data.get("subscribable", False)
        if vertical == "consumer_goods" and subscribable is True:
            logger.info(f"Category {category_id} [Redis] consumer_goods+subscribable → is_catalog_only=True")
            return True

        # Signal 1b: catalog_listing (only present in individual API response, not in /all)
        catalog_listing = settings_data.get("catalog_listing", None)
        if catalog_listing == "force":
            logger.info(f"Category {category_id} [Redis] catalog_listing=force → is_catalog_only=True")
            return True
        if catalog_listing is not None:
            # Present and not "force" → safe to skip API call
            logger.info(f"Category {category_id} [Redis] catalog_listing={catalog_listing!r} → is_catalog_only=False")
            return False
        # catalog_listing absent → need API call to check
        logger.info(f"Category {category_id} [Redis] catalog_listing absent — checking MeLi API...")
    else:
        logger.info(f"Category {category_id} not in Redis cache — checking MeLi API...")

    # ── Step 2: MeLi API check ──
    try:
        async with httpx.AsyncClient() as client:
            cat_resp, attr_resp = await asyncio.gather(
                client.get(f"{MELI_API_BASE}/categories/{category_id}", timeout=10.0),
                client.get(f"{MELI_API_BASE}/categories/{category_id}/attributes", timeout=10.0),
            )

        if cat_resp.status_code != 200:
            logger.warning(f"Could not fetch category {category_id}: {cat_resp.status_code}")
            return False

        api_cat = cat_resp.json()
        api_settings = api_cat.get("settings", {})
        catalog_listing_setting = api_settings.get("catalog_listing", "not_allowed")

        if catalog_listing_setting == "force":
            logger.info(f"Category {category_id} [API] catalog_listing=force → is_catalog_only=True")
            return True

        # Check consumer_goods + subscribable from API response
        if api_settings.get("vertical") == "consumer_goods" and api_settings.get("subscribable") is True:
            logger.info(f"Category {category_id} [API] consumer_goods+subscribable → is_catalog_only=True")
            return True

        # Check FAMILY/FAMILY_NAME required in attributes
        requires_family_name = False
        if attr_resp.status_code == 200:
            attrs = attr_resp.json()
            for attr in attrs:
                attr_id = attr.get("id", "")
                tags = attr.get("tags", {})
                is_required = "required" in tags and tags["required"] is not False
                if is_required and attr_id in ("FAMILY", "FAMILY_NAME"):
                    requires_family_name = True
                    break

        if requires_family_name:
            logger.info(f"Category {category_id} [API] family_name required → is_catalog_only=True")
            return True

        logger.info(
            f"Category {category_id} [API] catalog_listing={catalog_listing_setting}, "
            f"family_name_required={requires_family_name} → is_catalog_only=False"
        )
        return False

    except Exception as e:
        logger.error(f"Error checking catalog-only for {category_id}: {e}")
        return False


async def _get_attr_values_cached(
    attr_id: str,
    client: httpx.AsyncClient,
) -> List[Dict[str, str]]:
    """
    Fetch the complete values list for a ML attribute (GET /attributes/{attr_id}).
    Results are cached in Redis for 7 days to avoid repeated ML API calls.

    Returns a list of {"id": ..., "name": ...} dicts, or [] on error.
    """
    cache_key = _CACHE_KEY_ATTR_VALUES.format(attr_id=attr_id)
    cached = await cache.get(cache_key)
    if cached:
        logger.debug(f"Attr {attr_id}: values from cache")
        return cached

    # Cache miss — fetch from ML API
    resp = await client.get(
        f"{MELI_API_BASE}/attributes/{attr_id}",
        timeout=8.0,
    )
    if resp.status_code != 200:
        return []

    raw_values = resp.json().get("values", [])
    values = [
        {"id": v["id"], "name": v["name"]}
        for v in raw_values
        if v.get("id") and v.get("name")
    ]

    if values:
        await cache.set(cache_key, values, ex=_CACHE_TTL_ATTR_VALUES)
        logger.debug(f"Attr {attr_id}: {len(values)} values cached")

    return values


async def get_category_attributes(category_id: str) -> List[MeliCategoryAttribute]:
    """
    Get the attributes for a ML category.
    Uses: GET /categories/{category_id}/attributes

    Returns:
    - Atributos requeridos (required / catalog_required) → is_optional=False
    - Atributos opcionales de ficha técnica → is_optional=True
      Solo se incluyen atributos con tags específicas de ML que los marcan como
      visibles en la UI de publicación de ML:
        - "variation_attribute" → define variantes del producto (Tipo de hoja, Color…)
        - "buy_unit_attribute"  → unidad de compra (Cantidad de packs, Rollos…)
      Este criterio es más restrictivo que "tiene allowed_values", lo que evita
      mostrar demasiados campos irrelevantes al usuario.

    Los atributos BRAND y MODEL se excluyen porque el frontend ya los maneja
    con campos dedicados.
    Los atributos internos (SELLER_SKU, GTIN, EAN, etc.) también se excluyen.
    """
    # Atributos internos/técnicos que no necesita llenar el usuario final
    _SKIP_IDS = {
        "SELLER_SKU", "ITEM_CONDITION", "GTIN", "MPN", "EAN", "UPC", "ISBN",
        "ALPHANUMERIC_MODEL", "PRODUCT_FAMILY", "FAMILY", "FAMILY_NAME",
        "CURRENCY", "FULFILLMENT_POLICY", "WARRANTY_TYPE", "WARRANTY_TIME",
        "CATALOG_PRODUCT_ID",
    }

    # Tags de ML que indican que el atributo es parte de la ficha técnica visible
    # (los que ML muestra en su UI de publicación como campos extra de la categoría).
    # Solo estas dos tags son suficientemente específicas para no incluir demasiados campos.
    _RELEVANT_TAGS = {
        "variation_attribute",   # define variantes visibles del producto (Tipo de hoja, Color…)
        "buy_unit_attribute",    # unidad de compra de la categoría (Cantidad de packs, Rollos…)
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{MELI_API_BASE}/categories/{category_id}/attributes",
                timeout=10.0,
            )

            if response.status_code != 200:
                logger.error(f"Failed to get category attributes for {category_id}: {response.status_code}")
                return []

            data = response.json()
            attributes: List[MeliCategoryAttribute] = []

            for attr in data:
                attr_id = attr.get("id", "")
                attr_name = attr.get("name", "")
                tags = attr.get("tags", {})
                value_type = attr.get("value_type", "string")

                # Skip BRAND and MODEL — handled separately by the frontend
                if attr_id in ("BRAND", "MODEL"):
                    continue

                # Skip purely internal attributes the user doesn't need to fill
                if attr_id in _SKIP_IDS:
                    continue

                # Determine if this attribute is required or catalog_required
                is_required = "required" in tags and tags["required"] is not False
                is_catalog_required = "catalog_required" in tags and tags["catalog_required"] is not False

                # Determine if this is an optional-but-relevant attribute.
                raw_values = attr.get("values", [])
                has_relevant_tag = bool(set(tags.keys()) & _RELEVANT_TAGS)

                is_optional = (
                    not is_required
                    and not is_catalog_required
                    and has_relevant_tag
                )

                # Skip attributes that are neither required nor relevant optional
                if not is_required and not is_catalog_required and not is_optional:
                    continue

                # Extract allowed values.
                # ML truncates values[] in /categories/{id}/attributes when there are many
                # options (e.g. COLOR may have hundreds but only 4 are returned inline).
                # Fetch the complete list from GET /attributes/{attr_id} when truncated.
                allowed_values = []
                for val in raw_values:
                    val_id = val.get("id", "")
                    val_name = val.get("name", "")
                    if val_id and val_name:
                        allowed_values.append({"id": val_id, "name": val_name})

                # Always fetch the complete values list for list-type attributes.
                # ML inline values[] in /categories/{id}/attributes are always truncated
                # (may return 4 even when there are 200+ options like colors).
                if value_type == "list":
                    try:
                        full_values = await _get_attr_values_cached(attr_id, client)
                        if full_values:
                            allowed_values = full_values
                            logger.debug(
                                f"Attr {attr_id}: {len(allowed_values)} values loaded"
                            )
                    except Exception as e:
                        logger.debug(f"Could not fetch full values for {attr_id}: {e}")

                # Check if attribute allows custom/free text values.
                # value_type="string" means free-text input (values[] are just suggestions).
                # value_type="list" means only predefined values are valid.
                allow_custom = (
                    value_type == "string"
                    or bool(tags.get("allow_custom_value", False))
                    or len(allowed_values) == 0
                )

                tooltip = attr.get("tooltip", "")
                hint = attr.get("hint", "")

                attributes.append(MeliCategoryAttribute(
                    id=attr_id,
                    name=attr_name,
                    value_type=value_type,
                    required=is_required,
                    catalog_required=is_catalog_required,
                    is_optional=is_optional,
                    is_variation_attribute="variation_attribute" in tags,
                    allow_custom_value=allow_custom,
                    allowed_values=allowed_values if allowed_values else None,
                    tooltip=tooltip or hint or None,
                    default_value=attr.get("default_value"),
                ))

            required_count = sum(1 for a in attributes if not a.is_optional)
            optional_count = sum(1 for a in attributes if a.is_optional)
            logger.info(
                f"Category {category_id}: {required_count} required + {optional_count} optional attributes found"
            )
            return attributes

    except Exception as e:
        logger.error(f"Error fetching category attributes: {e}")
        return []


# ============================================================
# Redis category cache — árbol completo de categorías MLM
# ============================================================

_CACHE_KEY_CATEGORIES = "meli:categories:{site_id}"
_CACHE_TTL_CATEGORIES = 7 * 24 * 3600  # 7 days

_CACHE_KEY_ATTR_VALUES = "meli:attr_values:{attr_id}"
_CACHE_TTL_ATTR_VALUES = 7 * 24 * 3600  # 7 days


async def _get_category_from_cache(category_id: str, site_id: str = "MLM") -> Optional[Dict]:
    """Look up a single category by ID in the in-memory cache. Returns None if not found."""
    cache_key = _CACHE_KEY_CATEGORIES.format(site_id=site_id)
    categories = await cache.get(cache_key)
    if not categories:
        return None
    for cat in categories:
        if cat.get("id") == category_id:
            return cat
    return None


def _flatten_category_tree(
    node: dict,
    path: List[str],
    result: List[Dict[str, str]],
) -> None:
    """
    Recursively traverse the ML category tree.
    Only leaf categories (no children) are added to result.
    path: list of ancestor names for breadcrumb display.
    """
    children = node.get("children_categories", [])
    cat_id = node.get("id", "")
    cat_name = node.get("name", "")
    current_path = path + [cat_name]

    if not children:
        # Leaf category — add to result
        result.append({
            "id": cat_id,
            "name": cat_name,
            "path": " > ".join(current_path),
            # catalog_listing is checked separately in async batches
        })
    else:
        for child in children:
            _flatten_category_tree(child, current_path, result)


async def sync_categories_to_cache(site_id: str = "MLM") -> int:
    """
    Download the full ML category tree for the given site, filter out
    catalog-only leaf categories, and cache the result in Redis.

    Uses: GET /sites/{site_id}/categories/all

    The endpoint returns a flat dict: { "MLM1234": {id, name, path_from_root, ...}, ... }
    We extract leaf categories (those with no children in the dict or whose children
    don't appear as keys) and build the breadcrumb path from path_from_root.

    Returns the number of non-catalog leaf categories stored.
    """
    logger.info(f"[sync_categories] Starting full category sync for {site_id}...")

    # Step 1: Download flat category map
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{MELI_API_BASE}/sites/{site_id}/categories/all",
                timeout=60.0,
            )
        if resp.status_code != 200:
            logger.error(f"[sync_categories] Failed to fetch all categories: {resp.status_code}")
            return 0
        data = resp.json()
    except Exception as e:
        logger.error(f"[sync_categories] Error fetching all categories: {e}")
        return 0

    # Step 2: The API returns a flat dict {cat_id: cat_obj, ...}
    # Save ALL categories (leaves and intermediates) with the complete object as-is.
    # Add a computed "path" field (breadcrumb string) for easy text search.
    # catalog_listing check is done on-demand when the user selects a category.
    all_categories: List[Dict] = []

    if isinstance(data, dict):
        for cat_id, cat_obj in data.items():
            if not isinstance(cat_obj, dict):
                continue
            # Build breadcrumb from path_from_root for text search
            path_from_root = cat_obj.get("path_from_root", [])
            path = " > ".join(p.get("name", "") for p in path_from_root) or cat_obj.get("name", "")
            # Save full object + path
            entry = dict(cat_obj)
            entry["path"] = path
            all_categories.append(entry)
    elif isinstance(data, list):
        # Fallback: old tree format — flatten and save complete objects
        for root in data:
            _flatten_category_tree(root, [], all_categories)

    logger.info(f"[sync_categories] Found {len(all_categories)} categories (all levels), saving to Redis...")

    # Step 3: Save all to in-memory cache — no filtering, catalog check is done on-demand
    cache_key = _CACHE_KEY_CATEGORIES.format(site_id=site_id)
    await cache.set(cache_key, all_categories, ex=_CACHE_TTL_CATEGORIES)
    logger.info(f"[sync_categories] Saved {len(all_categories)} categories to cache")

    return len(all_categories)


async def search_categories_in_cache(
    query: str,
    site_id: str = "MLM",
    limit: int = 20,
) -> List[Dict[str, str]]:
    """
    Search the cached ML category tree in Redis by text (id or name match).
    Returns up to `limit` matching categories, each with id, name, and path.

    If the cache is empty, returns an empty list with a flag so the caller
    can inform the user to run the sync first.
    """
    cache_key = _CACHE_KEY_CATEGORIES.format(site_id=site_id)
    categories = await cache.get(cache_key)

    if not categories:
        logger.warning("[search_categories_in_cache] Cache is empty — run sync first")
        return []

    # Case-insensitive search in id, name, and path
    q_lower = query.lower().strip()
    matches = []
    for cat in categories:
        if (
            q_lower in cat.get("name", "").lower()
            or q_lower in cat.get("id", "").lower()
            or q_lower in cat.get("path", "").lower()
        ):
            matches.append(cat)
        if len(matches) >= limit:
            break

    return matches


async def get_or_fetch_category(category_id: str, site_id: str = "MLM") -> Optional[Dict]:
    """
    Get a single category by ID.
    1. First looks in the Redis bulk cache (meli:categories:{site_id}) for an exact id match.
    2. If not found, fetches from MeLi API (GET /categories/{id}), adds the 'path' field,
       and appends it to the Redis cache so future lookups are instant.

    Returns the full category object (same structure as meli:categories:MLM entries),
    or None if the category does not exist on MeLi.
    """
    cache_key = _CACHE_KEY_CATEGORIES.format(site_id=site_id)
    categories = await cache.get(cache_key)

    # Search in existing cache by exact id
    if categories:
        for cat in categories:
            if cat.get("id") == category_id:
                return cat

    # Not found in cache — fetch from MeLi API
    logger.info(f"[get_or_fetch_category] {category_id} not in cache, fetching from MeLi...")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{MELI_API_BASE}/categories/{category_id}",
                timeout=10.0,
            )
        if resp.status_code != 200:
            logger.warning(f"[get_or_fetch_category] MeLi returned {resp.status_code} for {category_id}")
            return None

        cat_obj = resp.json()
        # Add computed path field for consistency with cached entries
        path_from_root = cat_obj.get("path_from_root", [])
        cat_obj["path"] = " > ".join(p.get("name", "") for p in path_from_root) or cat_obj.get("name", "")

        # Append to in-memory cache so subsequent lookups are instant
        if categories:
            categories.append(cat_obj)
            ttl = await cache.ttl(cache_key)
            ttl = ttl if ttl > 0 else _CACHE_TTL_CATEGORIES
            await cache.set(cache_key, categories, ex=ttl)
            logger.info(f"[get_or_fetch_category] Appended {category_id} to cache")

        return cat_obj

    except Exception as e:
        logger.error(f"[get_or_fetch_category] Error fetching {category_id} from MeLi: {e}")
        return None


async def get_categories_cache_status(site_id: str = "MLM") -> Dict[str, Any]:
    """
    Return metadata about the category cache in Redis.
    """
    cache_key = _CACHE_KEY_CATEGORIES.format(site_id=site_id)
    categories = await cache.get(cache_key)

    if not categories:
        return {"cached": False, "count": 0, "ttl_seconds": 0}

    ttl = await cache.ttl(cache_key)
    return {
        "cached": True,
        "count": len(categories),
        "ttl_seconds": ttl,
        "site_id": site_id,
    }
