"""Mercado Libre router - OAuth, categories, publishing, and integration."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.dependencies import get_current_user, get_db
from app.models.listing import MeliListing
from app.models.meli_token import MeliToken
from app.services.image_storage import delete_images, get_image_paths
from app.schemas.meli import (
    MeliAuthUrlResponse,
    MeliCategoryAttributesResponse,
    MeliCategoryListResponse,
    MeliPublishRequest,
    MeliPublishResponse,
    MeliPublishVariantsRequest,
    MeliTokenStatus,
)
from app.schemas.listing import ListingVariantResult, MeliPublishBulkResponse
from app.services.meli_auth import exchange_code_for_tokens, get_auth_url
from app.services.meli_categories import get_categories_cache_status, get_category_attributes, get_category_children, get_category_siblings, get_or_fetch_category, get_parent_category, get_site_categories, predict_category, search_catalog_product, search_categories_by_text, search_categories_in_cache, sync_categories_to_cache, translate_to_spanish
from app.services.meli_client import close_item, get_user_info, publish_item, publish_item_catalog, update_item, update_stock, upload_pictures_to_meli

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meli", tags=["Mercado Libre"])


def _is_catalog_required_error(error_data: dict) -> bool:
    """
    Detecta si el error de ML indica que la categoría requiere modo catálogo obligatorio.

    ML devuelve señales específicas cuando rechaza una publicación porque la categoría
    exige catalog_listing=force. Estas señales son distintas a errores normales de datos
    inválidos (brand incorrecto, family_name inválido, palabras prohibidas, etc.).

    Señales detectadas:
    1. error == "invalid_op" → ML usa este código exclusivamente para indicar que la
       operación no es válida en esta categoría (requiere catalog_product_id).
    2. cause[].code == "body.invalid_fields" Y message contiene "catalog" → ML indica
       explícitamente que la categoría requiere un producto de catálogo.

    IMPORTANTE: La combinación message="body.invalid_fields" + "title" en el error string
    NO se usa como señal. ML también devuelve ese patrón cuando family_name o BRAND
    contienen valores inválidos (ej. "Amazon's Choice: Overall Pick"), lo que causaría
    una eliminación incorrecta del listing. La corrección de brand/family_name inválido
    se hace en meli_client.py antes de enviar el payload.

    Errores de datos inválidos usan codes como:
      - "title.invalid_words", "title.blacklisted_word", "title.max_length"
      - "body.invalid_fields" (también para brand/family_name inválido)
    Estos NO se confunden con los de catálogo.

    NOTA: body.required_fields + family_name NO se incluye aquí. Esa señal se maneja
    por separado en el router como "categoría demasiado genérica", sin borrar el listing.
    """
    # Señal 1: "invalid_op" es exclusivo de operaciones inválidas por modo catálogo
    if error_data.get("error") == "invalid_op":
        return True

    # Señal 2: causes con código de campo inválido y mención explícita de "catalog"
    _CATALOG_CAUSE_CODES = {
        "body.invalid_fields",
    }
    for cause in error_data.get("cause", []):
        code = cause.get("code", "")
        msg = cause.get("message", "").lower()
        if code in _CATALOG_CAUSE_CODES and "catalog" in msg:
            return True

    return False


def _is_family_name_required_error(error_data: dict) -> bool:
    """
    Detecta si ML rechazó la publicación porque la categoría exige 'family_name'
    (categoría genérica tipo "Otros"). Este error es diferente al de catálogo:
    la categoría no tiene catalog_listing=force, pero ML la trata como una familia
    de productos y requiere el campo family_name para agrupar variantes.

    En este caso NO se borra el listing — solo se devuelve un error 400 indicando
    que la categoría es demasiado genérica y se deben explorar alternativas más
    específicas.
    """
    for cause in error_data.get("cause", []):
        code = cause.get("code", "")
        msg = cause.get("message", "").lower()
        if code == "body.required_fields" and "family_name" in msg:
            return True
    return False


@router.get("/auth", response_model=MeliAuthUrlResponse)
async def get_meli_auth_url(
    current_user = Depends(get_current_user),
):
    """Get the Mercado Libre OAuth authorization URL for the current user."""
    auth_url = await get_auth_url(current_user.id)
    return MeliAuthUrlResponse(auth_url=auth_url)


def _build_callback_html(success: bool, message: str) -> str:
    """Build a styled HTML page for the OAuth callback result."""
    icon = "✅" if success else "❌"
    bg_color = "#0a0a0a"
    card_bg = "#1a1a2e" if success else "#2e1a1a"
    border_color = "#22c55e" if success else "#ef4444"
    text_color = "#e2e8f0"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mercado Libre - Conexión</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: {bg_color};
            color: {text_color};
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
        }}
        .card {{
            background: {card_bg};
            border: 1px solid {border_color};
            border-radius: 16px;
            padding: 48px;
            text-align: center;
            max-width: 480px;
            width: 90%;
            box-shadow: 0 0 40px rgba(0,0,0,0.3);
        }}
        .icon {{ font-size: 64px; margin-bottom: 24px; }}
        .message {{
            font-size: 18px;
            line-height: 1.6;
            color: {text_color};
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{icon}</div>
        <p class="message">{message}</p>
    </div>
</body>
</html>"""


@router.get("/callback", response_class=HTMLResponse)
async def meli_callback(
    code: str = Query(...),
    state: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Mercado Libre OAuth callback.
    Exchanges the authorization code for access/refresh tokens.
    Returns an HTML page with the result.
    """
    if not state:
        return HTMLResponse(
            content=_build_callback_html(False, "Parámetro de estado vacío."),
            status_code=400,
        )

    token = await exchange_code_for_tokens(db, state, code)
    if token is None:
        return HTMLResponse(
            content=_build_callback_html(
                False,
                "No se pudo conectar con Mercado Libre. Por favor, inténtalo de nuevo desde Configuración.",
            ),
            status_code=400,
        )

    return HTMLResponse(
        content=_build_callback_html(
            True,
            "Se realizó la conexión satisfactoriamente, ahora puedes cerrar esta ventana.",
        )
    )


@router.get("/status", response_model=MeliTokenStatus)
async def get_meli_status(
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if the current user has connected their ML account."""
    query = select(MeliToken).where(MeliToken.user_id == current_user.id)
    token = (await db.execute(query)).scalar_one_or_none()

    if token is None:
        return MeliTokenStatus(connected=False)

    return MeliTokenStatus(
        connected=True,
        meli_user_id=token.meli_user_id,
        expires_at=token.expires_at,
        token_type=token.token_type,
    )


@router.get("/me")
async def get_meli_user_info(
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the ML user profile info for the connected account."""
    user_info = await get_user_info(db, current_user.id)
    if user_info is None:
        raise HTTPException(
            status_code=400,
            detail="Not connected to Mercado Libre or token invalid",
        )
    return user_info


@router.get("/categories", response_model=MeliCategoryListResponse)
async def get_meli_categories(
    parent_id: Optional[str] = Query(None, description="Parent category ID for children"),
    current_user = Depends(get_current_user),
):
    """
    Get ML categories.
    Without parent_id: returns top-level categories.
    With parent_id: returns children of that category.
    """
    if parent_id:
        categories = await get_category_children(parent_id)
    else:
        categories = await get_site_categories()

    return MeliCategoryListResponse(
        categories=categories,
        site_id=settings.meli_site_id,
    )


@router.get("/categories/predict")
async def predict_meli_category(
    title: str = Query(..., min_length=5, description="Product title to predict category"),
    current_user = Depends(get_current_user),
):
    """
    Predict the best ML category for a product title.
    Translates the title to Spanish first for better prediction accuracy.
    Returns the predicted category ID and the translated title (useful for pre-filling
    the listing title field in the frontend).
    """
    # Translate title to Spanish for better ML category prediction
    translated_title = await translate_to_spanish(title)

    category_id = await predict_category(title)
    if category_id is None:
        return {
            "predicted_category_id": None,
            "translated_title": translated_title,
            "message": "Could not predict category",
        }
    return {
        "predicted_category_id": category_id,
        "translated_title": translated_title,
    }


@router.get("/categories/search", response_model=MeliCategoryListResponse)
async def search_meli_categories(
    q: str = Query(..., min_length=3, description="Search query for category name"),
    limit: int = Query(10, ge=1, le=20, description="Max number of results"),
    current_user = Depends(get_current_user),
):
    """
    Search ML categories by text using the domain_discovery endpoint.
    Returns leaf categories matching the query, sorted by relevance.
    Use this to find a category ID by name (e.g. 'teclado', 'auriculares').
    """
    categories = await search_categories_by_text(
        query=q,
        site_id=settings.meli_site_id,
        limit=limit,
    )
    return MeliCategoryListResponse(
        categories=categories,
        site_id=settings.meli_site_id,
    )


@router.get("/categories/siblings", response_model=MeliCategoryListResponse)
async def get_meli_category_siblings(
    category_id: str = Query(..., description="Category ID to get siblings for"),
    current_user = Depends(get_current_user),
):
    """
    Get sibling categories (other children of the same parent).
    Useful to suggest alternative categories when one is catalog-only.
    """
    siblings = await get_category_siblings(category_id)
    return MeliCategoryListResponse(
        categories=siblings,
        site_id=settings.meli_site_id,
    )


@router.get("/categories/browse")
async def browse_meli_categories(
    q: str = Query(..., min_length=2, description="Texto para buscar en las categorías cacheadas"),
    limit: int = Query(20, ge=1, le=50),
    current_user = Depends(get_current_user),
):
    """
    Busca categorías de ML en el caché de Redis.
    Mucho más rápido que domain_discovery porque no hace llamadas externas.
    Requiere haber ejecutado POST /meli/categories/sync al menos una vez.

    Retorna categorías con id, name, y path (ruta completa del árbol).
    Todas las categorías retornadas son NO de catálogo obligatorio.
    """
    results = await search_categories_in_cache(
        query=q,
        site_id=settings.meli_site_id,
        limit=limit,
    )
    cache_status = await get_categories_cache_status(site_id=settings.meli_site_id)
    return {
        "results": results,
        "total": len(results),
        "cache": cache_status,
        "cache_ready": cache_status.get("cached", False),
    }


@router.get("/categories/cache-status")
async def get_meli_categories_cache_status(
    current_user = Depends(get_current_user),
):
    """
    Retorna el estado del caché de categorías de ML en Redis.
    Muestra si está disponible, cuántas categorías hay, y cuándo expira.
    """
    status = await get_categories_cache_status(site_id=settings.meli_site_id)
    return status


@router.get("/categories/{category_id}")
async def get_meli_category(
    category_id: str,
    current_user = Depends(get_current_user),
):
    """
    Get a single ML category by ID with its full object (settings, path, etc.).
    First looks in the Redis cache (meli:categories:MLM).
    If not found in cache, fetches from MeLi API and saves it to Redis.

    The frontend can use this to:
    - Display the selected category's path/name
    - Access settings (buying_modes, item_conditions, max_title_length, etc.)
    - Show the full breadcrumb via the 'path' field
    """
    category = await get_or_fetch_category(category_id, site_id=settings.meli_site_id)
    if category is None:
        raise HTTPException(status_code=404, detail=f"Category '{category_id}' not found")
    return category


@router.get("/categories/{category_id}/attributes", response_model=MeliCategoryAttributesResponse)
async def get_meli_category_attributes(
    category_id: str,
    current_user = Depends(get_current_user),
):
    """
    Get the required attributes for a ML category.
    Returns only required and catalog_required attributes with their allowed values.
    Use this before publishing to know which fields the category needs.
    """
    attributes = await get_category_attributes(category_id)
    return MeliCategoryAttributesResponse(
        category_id=category_id,
        attributes=attributes,
    )


@router.post("/publish", response_model=MeliPublishResponse)
async def publish_listing_to_meli(
    data: MeliPublishRequest,
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish a listing to Mercado Libre.
    The listing must exist and belong to the current user.
    The user must have a connected ML account.
    """
    # Get the listing
    query = select(MeliListing).where(
        MeliListing.id == data.listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    if listing.meli_item_id:
        raise HTTPException(status_code=400, detail="Listing already published on ML")

    if not listing.meli_category_id:
        raise HTTPException(status_code=400, detail="Listing must have a ML category set before publishing")

    # ──────────────────────────────────────────────────────────────────────
    # PRE-VALIDACIÓN DE CATEGORÍA: Verificar ANTES de subir imágenes o
    # hacer cualquier operación costosa si la categoría es compatible con
    # publicaciones normales (no-catálogo). Esto evita crear drafts y
    # subir imágenes innecesariamente cuando la categoría va a fallar.
    # ──────────────────────────────────────────────────────────────────────
    from app.services.meli_categories import is_catalog_only_category as _is_cat_only
    is_catalog_only = await _is_cat_only(listing.meli_category_id)
    if is_catalog_only and not data.family_name:
        logger.warning(
            f"[PRE-VALIDATION] Category {listing.meli_category_id} is catalog-only or requires "
            f"family_name. Rejecting before image upload. Listing {listing.id} will be cleaned up."
        )
        # Limpiar el listing draft para que el usuario pueda reintentar
        try:
            await db.rollback()
            await db.delete(listing)
            await db.commit()
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up draft listing {listing.id}: {cleanup_err}")
            await db.rollback()

        # Buscar categorías hermanas como alternativas
        sibling_categories = []
        parent = None
        current_id = listing.meli_category_id
        for _ in range(3):
            parent = await get_parent_category(current_id)
            if not parent:
                break
            siblings = await get_category_children(parent["id"])
            candidates = [
                {"id": s.id, "name": s.name}
                for s in siblings
                if s.id != listing.meli_category_id
            ]
            if candidates:
                sibling_categories = candidates
                break
            current_id = parent["id"]

        raise HTTPException(status_code=400, detail={
            "code": "catalog_required_category",
            "message": (
                f"La categoría '{listing.meli_category_id}' requiere modo catálogo o family_name "
                "y no es compatible con publicaciones normales. "
                "Selecciona una categoría alternativa más específica, o si la categoría es genérica "
                "(tipo 'Otros'), ingresa un 'Family Name' en el formulario."
            ),
            "parent_category_id": parent["id"] if parent else None,
            "parent_category_name": parent["name"] if parent else None,
            "alternative_categories": sibling_categories,
        })

    # ──────────────────────────────────────────────────────────────────────
    # PRE-VALIDACIÓN: Verificar que la categoría sea leaf (hoja del árbol).
    # ML rechaza publicaciones en categorías intermedias con el error
    # item.category_id.invalid: "Make sure you're posting in a leaf category".
    # Validamos aquí antes de subir imágenes u otras operaciones costosas.
    # ──────────────────────────────────────────────────────────────────────
    children = await get_category_children(listing.meli_category_id)
    if children:
        raise HTTPException(status_code=400, detail={
            "code": "non_leaf_category",
            "message": (
                f"La categoría seleccionada no es válida para publicar directamente. "
                f"Debes elegir una subcategoría más específica."
            ),
            "children": [{"id": c.id, "name": c.name} for c in children],
        })

    # Get product images from local filesystem
    product_id = data.product_id or listing.product_id
    pictures = get_image_paths(product_id) if product_id else []

    # ML requires at least 1 image to publish
    if not pictures:
        raise HTTPException(
            status_code=400,
            detail=(
                "El producto no tiene imágenes. "
                "Usa 'Actualizar datos de Amazon' para descargar las imágenes antes de publicar."
            ),
        )

    # Build dynamic attributes for ML
    brand = data.brand or data.product_brand or None
    model = data.model or (data.product_title[:60] if data.product_title else listing.title[:60])

    # Convert dynamic attributes from request to the format publish_item expects
    extra_attributes = []
    if data.attributes:
        for attr in data.attributes:
            attr_dict = {"id": attr.id}
            if attr.value_id:
                attr_dict["value_id"] = attr.value_id
            if attr.value_name:
                attr_dict["value_name"] = attr.value_name
            extra_attributes.append(attr_dict)

    # Auto-inject GTIN con el ASIN del producto solo si:
    #   1. El frontend NO envió el atributo GTIN en absoluto (ni vacío), Y
    #   2. EMPTY_GTIN_REASON no está presente (el usuario no indicó explícitamente que no tiene GTIN)
    #
    # Si el frontend envía GTIN (incluso con valor vacío), se respeta esa decisión.
    # El campo GTIN ahora es explícito y visible en el formulario — el usuario tiene
    # control total sobre su valor. El backend solo hace el fallback cuando el campo
    # no fue enviado en absoluto (compatibilidad hacia atrás con publicaciones antiguas).
    has_gtin = any(a.get("id") == "GTIN" for a in extra_attributes)
    has_empty_gtin_reason = any(a.get("id") == "EMPTY_GTIN_REASON" for a in extra_attributes)
    if not has_gtin and not has_empty_gtin_reason and data.product_asin:
        logger.info(
            f"[GTIN] Auto-injecting GTIN with ASIN '{data.product_asin}' "
            f"for listing {listing.id} (category {listing.meli_category_id}) — "
            "frontend did not send GTIN explicitly"
        )
        extra_attributes.append({"id": "GTIN", "value_name": data.product_asin})
    elif has_gtin:
        # El frontend envió GTIN — verificar si tiene valor real o está vacío
        gtin_attr = next((a for a in extra_attributes if a.get("id") == "GTIN"), None)
        gtin_value = (gtin_attr or {}).get("value_name", "").strip()
        if not gtin_value:
            # GTIN enviado pero vacío — eliminar GTIN del listado y auto-inyectar
            # EMPTY_GTIN_REASON para que ML no rechace por falta de código de barras.
            extra_attributes = [a for a in extra_attributes if a.get("id") != "GTIN"]
            if not any(a.get("id") == "EMPTY_GTIN_REASON" for a in extra_attributes):
                extra_attributes.append({"id": "EMPTY_GTIN_REASON", "value_name": "El producto no tiene código registrado"})
                logger.info(
                    f"[GTIN] Frontend envió GTIN vacío para listing {listing.id} — "
                    "se omite GTIN y se auto-inyecta EMPTY_GTIN_REASON='El producto no tiene código registrado'."
                )
            else:
                logger.info(
                    f"[GTIN] Frontend envió GTIN vacío para listing {listing.id} — se omite el atributo."
                )
        else:
            logger.info(
                f"[GTIN] Frontend envió GTIN explícito: '{gtin_value}' para listing {listing.id}"
            )

    if not extra_attributes:
        extra_attributes = None

    # Build sale_terms from warranty data
    sale_terms = None
    if data.warranty_type:
        warranty_value = data.warranty_type
        if data.warranty_time:
            warranty_value = f"{data.warranty_type}: {data.warranty_time}"
        sale_terms = [
            {"id": "WARRANTY_TYPE", "value_name": data.warranty_type},
            {"id": "WARRANTY_TIME", "value_name": data.warranty_time or ""},
        ]

    # Add manufacturing time (product availability days) if provided
    if data.manufacturing_time:
        if sale_terms is None:
            sale_terms = []
        sale_terms.append({"id": "MANUFACTURING_TIME", "value_name": f"{data.manufacturing_time} días"})

    # Build shipping config
    shipping = None
    if data.shipping_mode or data.free_shipping is not None:
        shipping = {}
        if data.shipping_mode:
            shipping["mode"] = data.shipping_mode
        if data.free_shipping is not None:
            shipping["free_shipping"] = data.free_shipping

    # Always attempt normal (non-catalog) publish.
    # We never fall back to catalog mode automatically — if ML requires catalog,
    # we return an error with alternative categories so the user can choose.
    # Convert VariationPublishItem list to plain dicts for publish_item()
    variations_payload = None
    if data.variations:
        variations_payload = [
            {
                "asin": v.asin,
                "attributes": v.attributes,
                "display_labels": v.display_labels,
                "price": v.price,
                "available_quantity": v.available_quantity,
                "pictures": v.pictures,
            }
            for v in data.variations
        ]

    result = await publish_item(
        db=db,
        user_id=current_user.id,
        title=listing.title,
        category_id=listing.meli_category_id,
        price=float(listing.meli_price),
        available_quantity=listing.available_quantity,
        listing_type_id=listing.listing_type,
        condition=data.condition or "new",
        description=listing.description or "",
        pictures=pictures,
        brand=brand,
        model=model,
        extra_attributes=extra_attributes,
        family_name=data.family_name or None,
        sale_terms=sale_terms,
        shipping=shipping,
        variations=variations_payload,
    )

    if result is None:
        raise HTTPException(status_code=500, detail="No response from ML API")

    if result.get("error"):
        # Normal publish failed — parse ML error response to give better feedback
        detail_text = result.get("detail", "Unknown error")
        try:
            import json
            error_data = json.loads(detail_text) if isinstance(detail_text, str) else detail_text

            # Detect catalog-required category: ML rejects the item because the category
            # requires catalog mode. We do NOT fall back to catalog — instead we clean up
            # and suggest alternative categories.
            if isinstance(error_data, dict):
                # ----------------------------------------------------------------
                # Detectar categoría demasiado genérica (family_name required):
                # ML exige family_name en categorías tipo "Otros" porque las trata
                # como familias de productos. Esto NO es catalog_listing=force, por
                # lo que el listing NO se borra — solo se informa al usuario que
                # debe elegir una categoría más específica.
                # ----------------------------------------------------------------
                if _is_family_name_required_error(error_data):
                    logger.warning(
                        f"[FAMILY_NAME REQUIRED] Category {listing.meli_category_id} "
                        "requires family_name — too generic, suggesting alternatives. "
                        "Cleaning up draft listing."
                    )
                    # Limpiar el listing draft para que el usuario pueda reintentar
                    # Product status cleanup handled by frontend
                    try:
                        await db.delete(listing)
                        await db.commit()
                    except Exception as cleanup_err:
                        logger.error(f"Failed to clean up draft listing {listing.id} (family_name): {cleanup_err}")

                    # Buscar categorías hermanas como alternativas
                    sibling_categories = []
                    parent = None
                    current_id = listing.meli_category_id
                    for _ in range(3):
                        parent = await get_parent_category(current_id)
                        if not parent:
                            break
                        siblings = await get_category_children(parent["id"])
                        candidates = [
                            {"id": s.id, "name": s.name}
                            for s in siblings
                            if s.id != listing.meli_category_id
                        ]
                        if candidates:
                            sibling_categories = candidates
                            break
                        current_id = parent["id"]

                    raise HTTPException(status_code=400, detail={
                        "code": "generic_category",
                        "message": (
                            f"La categoría '{listing.meli_category_id}' es demasiado genérica "
                            "(ML requiere 'family_name' para agrupar variantes en esta categoría). "
                            "Por favor selecciona una categoría más específica para tu producto."
                        ),
                        "ml_error": error_data,
                        "parent_category_id": parent["id"] if parent else None,
                        "parent_category_name": parent["name"] if parent else None,
                        "alternative_categories": sibling_categories,
                    })

                if _is_catalog_required_error(error_data):
                    # Log full category info to help diagnose which categories are catalog-only
                    try:
                        import httpx as _httpx
                        _cat_resp = await _httpx.AsyncClient().get(
                            f"https://api.mercadolibre.com/categories/{listing.meli_category_id}",
                            timeout=8.0,
                        )
                        if _cat_resp.status_code == 200:
                            _cat_data = _cat_resp.json()
                            _path = " > ".join(p.get("name", "") for p in _cat_data.get("path_from_root", []))
                            _cl_setting = _cat_data.get("settings", {}).get("catalog_listing", "?")
                            logger.warning(
                                f"[CATALOG REQUIRED] Category {listing.meli_category_id}\n"
                                f"  path           : {_path}\n"
                                f"  catalog_listing: {_cl_setting}\n"
                                f"  children       : {len(_cat_data.get('children_categories', []))}"
                            )
                        # Log required attributes for this category
                        _attr_resp = await _httpx.AsyncClient().get(
                            f"https://api.mercadolibre.com/categories/{listing.meli_category_id}/attributes",
                            timeout=8.0,
                        )
                        if _attr_resp.status_code == 200:
                            _attrs = _attr_resp.json()
                            _required = [
                                f"{a.get('id')} ({a.get('name')})"
                                for a in _attrs
                                if "required" in a.get("tags", {}) or "catalog_required" in a.get("tags", {})
                            ]
                            logger.warning(
                                f"[CATALOG REQUIRED] Required attributes for {listing.meli_category_id}: "
                                + (", ".join(_required) if _required else "none")
                            )
                    except Exception as _diag_err:
                        logger.warning(f"[CATALOG REQUIRED] Could not fetch category details: {_diag_err}")

                    logger.info(
                        f"Category {listing.meli_category_id} requires catalog mode. "
                        "Cleaning up and returning alternative categories."
                    )
                    # Revert product status and clean up the draft listing
                    # Product status cleanup handled by frontend
                    try:
                        await db.delete(listing)
                        await db.commit()
                    except Exception:
                        pass

                    # Walk up the tree to find sibling categories as alternatives
                    sibling_categories = []
                    parent = None
                    current_id = listing.meli_category_id
                    for _ in range(3):
                        parent = await get_parent_category(current_id)
                        if not parent:
                            break
                        siblings = await get_category_children(parent["id"])
                        candidates = [
                            {"id": s.id, "name": s.name}
                            for s in siblings
                            if s.id != listing.meli_category_id
                        ]
                        if candidates:
                            sibling_categories = candidates
                            break
                        current_id = parent["id"]

                    if sibling_categories:
                        raise HTTPException(status_code=400, detail={
                            "code": "catalog_required_category",
                            "message": (
                                f"La categoría '{listing.meli_category_id}' requiere modo catálogo "
                                "y no está disponible para publicaciones personalizadas. "
                                "Por favor selecciona una categoría alternativa."
                            ),
                            "ml_error": error_data,
                            "parent_category_id": parent["id"] if parent else None,
                            "parent_category_name": parent["name"] if parent else None,
                            "alternative_categories": sibling_categories,
                        })
                    else:
                        raise HTTPException(status_code=400, detail={
                            "code": "catalog_required_category",
                            "message": (
                                f"La categoría '{listing.meli_category_id}' y toda su jerarquía "
                                "requieren modo catálogo y no están disponibles para "
                                "publicaciones personalizadas. "
                                "Por favor ingresa manualmente el ID de una categoría diferente."
                            ),
                            "ml_error": error_data,
                            "parent_category_id": None,
                            "parent_category_name": None,
                            "alternative_categories": [],
                        })

            if isinstance(error_data, dict) and "cause" in error_data:
                # Extract only actual errors (not warnings)
                errors = [c for c in error_data["cause"] if c.get("type") == "error"]
                warnings_list = [c for c in error_data["cause"] if c.get("type") == "warning"]

                if errors:
                    # ----------------------------------------------------------------
                    # Detectar error de atributo condicional requerido (ej: GTIN).
                    # ML devuelve este código cuando la categoría exige un atributo
                    # que no fue enviado o tiene valor inválido.
                    # Lo convertimos en un 400 con mensaje descriptivo y amigable.
                    # ----------------------------------------------------------------
                    missing_attr_errors = [
                        e for e in errors
                        if e.get("code") == "item.attribute.missing_conditional_required"
                    ]
                    if missing_attr_errors:
                        # Extraer los atributos faltantes de los mensajes de ML
                        missing_attr_messages = []
                        for e in missing_attr_errors:
                            msg = e.get("message", "")
                            missing_attr_messages.append(msg)

                        # Limpiar el listing draft para que el usuario pueda reintentar
                        logger.warning(
                            f"[MISSING ATTRS] Cleaning up draft listing {listing.id} "
                            f"due to missing required attributes: {missing_attr_messages}"
                        )
                        try:
                            await db.delete(listing)
                            await db.commit()
                        except Exception as cleanup_err:
                            logger.error(f"Failed to clean up draft listing {listing.id} (missing attrs): {cleanup_err}")

                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "missing_required_attributes",
                                "message": (
                                    f"La categoría '{listing.meli_category_id}' requiere atributos adicionales "
                                    "que no fueron enviados o tienen valores inválidos. "
                                    "Completa los campos requeridos en el formulario de publicación "
                                    "e inténtalo de nuevo."
                                ),
                                "errors": missing_attr_messages,
                                "ml_error": error_data,
                            }
                        )

                    # ----------------------------------------------------------------
                    # Detectar error de longitud mínima del título.
                    # ML rechaza títulos muy cortos o poco descriptivos.
                    # ----------------------------------------------------------------
                    title_errors = [
                        e for e in errors
                        if e.get("code") in (
                            "item.title.minimum_length",
                            "item.title.invalid_length",
                            "item.title.blacklisted_word",
                            "item.title.invalid_words",
                        )
                    ]
                    if title_errors:
                        title_error_messages = [e.get("message", "") for e in title_errors]
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "invalid_title",
                                "message": (
                                    "El título del producto no cumple con los requisitos de Mercado Libre. "
                                    "Asegúrate de incluir características importantes como marca, modelo o categoría "
                                    f"(mínimo ~20 caracteres descriptivos). Título actual: '{listing.title[:60]}'"
                                ),
                                "errors": title_error_messages,
                                "ml_error": error_data,
                            }
                        )

                    # Otros errores de validación genéricos
                    error_messages = [f"{e.get('code', 'unknown')}: {e.get('message', '')}" for e in errors]
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": "ML validation errors",
                            "errors": error_messages,
                            "warnings": [w.get("message", "") for w in warnings_list],
                        }
                    )
        except HTTPException:
            raise
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        # Generic ML error — clean up the orphan draft listing before raising
        logger.warning(f"Generic ML error for listing {listing.id}, cleaning up draft...")
        try:
            await db.delete(listing)
            await db.commit()
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up draft listing {listing.id}: {cleanup_err}")

        raise HTTPException(status_code=500, detail=f"ML publish failed: {detail_text}")

    # Update listing with ML data
    listing.meli_item_id = result.get("id")
    listing.meli_permalink = result.get("permalink")
    listing.status = "active"

    # Save ML picture IDs so we can reuse them without local filesystem
    ml_pictures = result.get("pictures", [])
    picture_ids = [p["id"] for p in ml_pictures if p.get("id")]
    if picture_ids:
        listing.meli_picture_ids = picture_ids

    await db.commit()

    # Clean up local images — ML already has them hosted
    if picture_ids and product_id:
        try:
            delete_images(product_id)
            logger.info(f"Cleaned up local images for product {product_id} after publish")
        except Exception as e:
            logger.warning(f"Failed to clean up local images for product {product_id}: {e}")

    # Collect any warnings from the response (convert dict objects to strings)
    warnings = None
    if result.get("warnings"):
        raw_warnings = result.get("warnings", [])
        warnings = [w.get("message", str(w)) if isinstance(w, dict) else str(w) for w in raw_warnings]

    # Variations feedback: inform caller how many variants were published and if any were dropped
    variations_dropped = bool(data.variations and data.family_name)
    variations_count = len(variations_payload) if variations_payload and not variations_dropped else 0

    return MeliPublishResponse(
        meli_item_id=listing.meli_item_id,
        permalink=listing.meli_permalink or "",
        status="active",
        product_status="published",
        warnings=warnings,
        variations_count=variations_count if data.variations else None,
        variations_dropped=variations_dropped if data.variations else None,
    )


@router.put("/update/{listing_id}")
async def update_listing_on_meli(
    listing_id: int,
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update an existing listing on Mercado Libre.
    Syncs price and images from the local listing/product to ML.
    """
    # Get the listing
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    if not listing.meli_item_id:
        raise HTTPException(status_code=400, detail="Listing has not been published to ML yet")

    # Build update payload (ML only allows certain fields to be updated)
    updates = {
        "price": float(listing.meli_price),
        "available_quantity": listing.available_quantity,
    }

    # Reuse saved ML picture IDs if available, otherwise re-upload from filesystem
    if listing.meli_picture_ids:
        updates["pictures"] = [{"id": pid} for pid in listing.meli_picture_ids]
    else:
        pictures = get_image_paths(listing.product_id) if listing.product_id else []
        if pictures:
            logger.info(f"Uploading {len(pictures)} images for listing update...")
            picture_entries, upload_warnings = await upload_pictures_to_meli(
                db, current_user.id, pictures
            )
            if picture_entries:
                updates["pictures"] = picture_entries
            if upload_warnings:
                logger.warning(f"Image upload warnings: {upload_warnings}")

    result = await update_item(
        db=db,
        user_id=current_user.id,
        meli_item_id=listing.meli_item_id,
        updates=updates,
    )

    if result is None or result.get("error"):
        detail = result.get("detail", "Unknown error") if result else "No response from ML"
        raise HTTPException(status_code=500, detail=f"ML update failed: {detail}")

    return {
        "message": "Listing updated on Mercado Libre",
        "meli_item_id": listing.meli_item_id,
        "permalink": listing.meli_permalink or "",
    }


@router.put("/stock/{listing_id}")
async def update_stock_on_meli(
    listing_id: int,
    quantity: int = Query(..., ge=0, description="New stock quantity"),
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update only the stock (available_quantity) of a listing on Mercado Libre.
    Also updates the local listing record.
    """
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    # Update local record
    listing.available_quantity = quantity

    # If published on ML, update remotely too
    if listing.meli_item_id:
        result = await update_stock(
            db=db,
            user_id=current_user.id,
            meli_item_id=listing.meli_item_id,
            quantity=quantity,
        )
        if result is None or result.get("error"):
            detail = result.get("detail", "Unknown error") if result else "No response from ML"
            raise HTTPException(status_code=500, detail=f"ML stock update failed: {detail}")

    await db.commit()

    return {
        "message": f"Stock actualizado a {quantity}",
        "meli_item_id": listing.meli_item_id,
        "available_quantity": quantity,
    }


@router.delete("/close/{listing_id}")
async def close_listing_on_meli(
    listing_id: int,
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Close/delete a listing on Mercado Libre permanently.
    Closes the item on ML API, then removes the local listing record.
    Product status update is handled by the frontend.
    """
    # Get the listing
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    if not listing.meli_item_id:
        # No ML item — just delete the local listing
        await db.delete(listing)
        await db.commit()
        return {"message": "Listing draft deleted", "product_status": "scraped"}

    # Close the item on ML
    result = await close_item(
        db=db,
        user_id=current_user.id,
        meli_item_id=listing.meli_item_id,
    )

    if result is None or result.get("error"):
        detail = result.get("detail", "Unknown error") if result else "No response from ML"
        raise HTTPException(status_code=500, detail=f"ML close failed: {detail}")

    # Remove the local listing
    await db.delete(listing)
    await db.commit()

    return {
        "message": "Publicación cerrada y eliminada de Mercado Libre",
        "meli_item_id": listing.meli_item_id,
        "product_status": "scraped",
    }


@router.get("/catalog/search")
async def search_ml_catalog(
    q: str = Query(..., min_length=3, description="Product title to search in ML catalog"),
    category: Optional[str] = Query(None, description="Category ID to narrow the search"),
    limit: int = Query(10, ge=1, le=20),
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Search products in the ML catalog by title (and optionally category).
    Returns a list of catalog products with catalog_product_id and name.
    Used when a category requires catalog mode (family_name obligatorio).
    Requires the user's ML access token.
    """
    from app.services.meli_auth import get_valid_token
    access_token = await get_valid_token(db, current_user.id)
    if not access_token:
        raise HTTPException(status_code=400, detail="Not connected to Mercado Libre")

    results = await search_catalog_product(
        title=q,
        category_id=category,
        site_id=settings.meli_site_id,
        limit=limit,
        access_token=access_token,
    )
    return {"results": results, "total": len(results)}


@router.post("/publish-catalog", response_model=MeliPublishResponse)
async def publish_catalog_listing_to_meli(
    listing_id: int = Query(..., description="Local listing ID to publish in catalog mode"),
    catalog_product_id: str = Query(..., description="ML catalog product ID (e.g. MLM123456)"),
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish a listing to Mercado Libre using catalog mode.
    Used when the category requires catalog_listing=force (family_name obligatorio).
    The listing must exist and belong to the current user.
    The catalog_product_id must be a valid ML catalog product.
    Product status update is handled by the frontend.
    """
    # Get the listing
    query = select(MeliListing).where(
        MeliListing.id == listing_id, MeliListing.user_id == current_user.id
    )
    listing = (await db.execute(query)).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    if listing.meli_item_id:
        raise HTTPException(status_code=400, detail="Listing already published on ML")

    result = await publish_item_catalog(
        db=db,
        user_id=current_user.id,
        catalog_product_id=catalog_product_id,
        price=float(listing.meli_price),
        available_quantity=listing.available_quantity,
        listing_type_id=listing.listing_type,
        condition="new",
        category_id=listing.meli_category_id,
    )

    if result is None:
        raise HTTPException(status_code=500, detail="No response from ML API")

    if result.get("error"):
        detail_text = result.get("detail", "Unknown error")
        raise HTTPException(status_code=400, detail=f"ML catalog publish failed: {detail_text}")

    # Update listing with ML data
    listing.meli_item_id = result.get("id")
    listing.meli_permalink = result.get("permalink")
    listing.status = "active"

    # Save ML picture IDs for catalog publish
    ml_pictures = result.get("pictures", [])
    picture_ids = [p["id"] for p in ml_pictures if p.get("id")]
    if picture_ids:
        listing.meli_picture_ids = picture_ids

    await db.commit()

    # Clean up local images — ML already has them hosted
    if picture_ids and listing.product_id:
        try:
            delete_images(listing.product_id)
            logger.info(f"Cleaned up local images for product {listing.product_id} after catalog publish")
        except Exception as e:
            logger.warning(f"Failed to clean up local images for product {listing.product_id}: {e}")

    return MeliPublishResponse(
        meli_item_id=listing.meli_item_id,
        permalink=listing.meli_permalink or "",
        status="active",
        product_status="published",
        warnings=None,
    )


@router.post("/categories/sync")
async def sync_meli_categories(
    current_user = Depends(get_current_user),
):
    """
    Descarga el árbol completo de categorías de ML México, filtra las de catálogo
    obligatorio, y guarda el resultado en Redis (TTL 7 días).

    Este proceso puede tardar varios minutos (miles de categorías × verificación de catálogo).
    Se recomienda ejecutarlo una vez y dejar que Redis expire automáticamente.

    Retorna el número de categorías disponibles guardadas.
    """
    # Run sync inline — categories download is fast enough for a single request.
    logger.info(f"Starting ML category sync for user {current_user.id}...")
    count = await sync_categories_to_cache(site_id=settings.meli_site_id)
    if count == 0:
        raise HTTPException(
            status_code=500,
            detail="No se pudieron sincronizar las categorías. Revisa los logs del backend."
        )
    return {
        "status": "ok",
        "message": f"Sincronización completada: {count} categorías disponibles guardadas en caché.",
        "count": count,
        "site_id": settings.meli_site_id,
    }


@router.delete("/disconnect")
async def disconnect_meli(
    current_user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect ML account (remove tokens)."""
    query = select(MeliToken).where(MeliToken.user_id == current_user.id)
    token = (await db.execute(query)).scalar_one_or_none()
    if token:
        await db.delete(token)
        await db.commit()
        return {"message": "Mercado Libre disconnected"}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers for publish-variants
# ──────────────────────────────────────────────────────────────────────────────

def _friendly_error(exc: Exception) -> str:
    """Convert technical exceptions to user-readable messages."""
    msg = str(exc)
    if "StringDataRightTruncation" in msg or "value too long" in msg:
        return "El título generado es demasiado largo. Acorta el título base del producto."
    if "greenlet_spawn" in msg or "await_only" in msg:
        return "Error interno de base de datos. Reintenta la publicación."
    if "UniqueViolation" in msg or "unique constraint" in msg.lower():
        return "Esta variante ya está publicada en Mercado Libre."
    if "ConnectionRefused" in msg or "connect" in msg.lower():
        return "No se pudo conectar a Mercado Libre. Verifica tu conexión."
    if "family_name" in msg.lower():
        return "Esta categoría requiere un 'Nombre de familia'. Completa ese campo en el formulario."
    if "required_fields" in msg.lower() or "required" in msg.lower():
        return f"Faltan campos requeridos por Mercado Libre: {msg[:120]}"
    if "invalid_op" in msg or "catalog" in msg.lower():
        return "Esta categoría solo permite publicaciones de catálogo. Selecciona otra categoría."
    if "title" in msg.lower() and ("invalid" in msg.lower() or "prohibited" in msg.lower()):
        return "El título contiene palabras no permitidas por Mercado Libre. Edita el título base."
    # Fallback: keep it short, no stacktrace
    return msg[:200] if len(msg) <= 200 else msg[:200] + "…"


def _get_dimension_display(attributes: dict, display_labels: dict, asin: str) -> str:
    """Return a short human-readable label for this variant (used in ML title suffix).
    Returns the attribute VALUE (e.g. 'Cacao Nib Crunch'), not the label ('Flavor').
    """
    priority = ["flavor_name", "scent_name", "color_name", "size_name", "style_name"]
    for key in priority:
        if key in attributes and attributes[key]:
            return attributes[key]
    # Use first available attribute value
    if attributes:
        first_key = next(iter(attributes))
        return attributes[first_key] or asin
    return asin


@router.post("/publish-variants", response_model=MeliPublishBulkResponse)
async def publish_variants_to_meli(
    data: MeliPublishVariantsRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish each variation as a separate ML item (1-to-N).
    Uses the base_listing_id draft for category, description, and price defaults.
    For each variation: creates/reuses a variant draft, uploads its images, and publishes.
    """
    if not data.variations:
        raise HTTPException(status_code=400, detail="No variations provided")

    # Get base listing (draft) for category / description / defaults
    base_listing = (await db.execute(
        select(MeliListing).where(
            MeliListing.id == data.base_listing_id,
            MeliListing.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if base_listing is None:
        raise HTTPException(status_code=404, detail="Base listing not found")

    # Get product images from local filesystem for fallback
    product_id = data.product_id or base_listing.product_id
    fallback_pictures = get_image_paths(product_id) if product_id else []

    base_title = base_listing.title
    brand = data.brand or data.product_brand or None

    # Build sale_terms
    sale_terms = None
    if data.warranty_type:
        sale_terms = [
            {"id": "WARRANTY_TYPE", "value_name": data.warranty_type},
            {"id": "WARRANTY_TIME", "value_name": data.warranty_time or ""},
        ]

    # Add manufacturing time (product availability days) if provided
    if data.manufacturing_time:
        if sale_terms is None:
            sale_terms = []
        sale_terms.append({"id": "MANUFACTURING_TIME", "value_name": f"{data.manufacturing_time} días"})

    # Build shipping
    shipping = None
    if data.shipping_mode or data.free_shipping is not None:
        shipping = {}
        if data.shipping_mode:
            shipping["mode"] = data.shipping_mode
        if data.free_shipping is not None:
            shipping["free_shipping"] = data.free_shipping

    # Build extra_attributes
    extra_attributes = []
    if data.attributes:
        for attr in data.attributes:
            attr_dict = {"id": attr.id}
            if attr.value_id:
                attr_dict["value_id"] = attr.value_id
            if attr.value_name:
                attr_dict["value_name"] = attr.value_name
            extra_attributes.append(attr_dict)
    # GTIN fallback
    has_gtin = any(a.get("id") == "GTIN" for a in extra_attributes)
    has_empty_gtin_reason = any(a.get("id") == "EMPTY_GTIN_REASON" for a in extra_attributes)
    if not has_gtin and not has_empty_gtin_reason and data.product_asin:
        extra_attributes.append({"id": "GTIN", "value_name": data.product_asin})
    elif has_gtin:
        gtin_attr = next((a for a in extra_attributes if a.get("id") == "GTIN"), None)
        if not (gtin_attr or {}).get("value_name", "").strip():
            extra_attributes = [a for a in extra_attributes if a.get("id") != "GTIN"]
            if not any(a.get("id") == "EMPTY_GTIN_REASON" for a in extra_attributes):
                extra_attributes.append({"id": "EMPTY_GTIN_REASON", "value_name": "El producto no tiene código registrado"})
    if not extra_attributes:
        extra_attributes = None

    results: list[ListingVariantResult] = []

    for variation in data.variations:
        variation_asin = variation.asin
        user_error: str | None = None
        dim_label: str = variation_asin  # fallback until computed
        try:
            # Build variant title: "Dim Label - Base Title" truncated to 60 chars (ML limit).
            # Variant name goes FIRST so it's always visible even when base is long.
            dim_label = _get_dimension_display(variation.attributes, variation.display_labels, variation_asin)
            ML_TITLE_LIMIT = 60
            separator = " - "
            # If dim_label alone exceeds the limit, truncate it
            dim_label_safe = dim_label[:ML_TITLE_LIMIT - len(separator) - 5].rstrip() if len(dim_label) > ML_TITLE_LIMIT - len(separator) - 5 else dim_label
            prefix = f"{dim_label_safe}{separator}"
            remaining = ML_TITLE_LIMIT - len(prefix)
            truncated_base = base_title[:remaining].rstrip() if remaining > 0 else ""
            variant_title = f"{prefix}{truncated_base}"

            # Use a savepoint so a failure here doesn't kill the whole session
            async with db.begin_nested():
                # 1. Find or create variant draft
                existing_variant = (await db.execute(
                    select(MeliListing).where(
                        MeliListing.product_id == base_listing.product_id,
                        MeliListing.user_id == current_user.id,
                        MeliListing.variation_asin == variation_asin,
                        MeliListing.meli_item_id.is_(None),
                    )
                )).scalar_one_or_none()

                if existing_variant:
                    existing_variant.title = variant_title
                    existing_variant.meli_price = variation.price
                    existing_variant.available_quantity = variation.available_quantity
                    existing_variant.meli_category_id = base_listing.meli_category_id
                    existing_variant.listing_type = base_listing.listing_type
                    existing_variant.description = base_listing.description
                    existing_variant.status = "draft"
                    variant_listing = existing_variant
                else:
                    variant_listing = MeliListing(
                        user_id=current_user.id,
                        product_id=base_listing.product_id,
                        title=variant_title,
                        description=base_listing.description,
                        meli_price=variation.price,
                        available_quantity=variation.available_quantity,
                        meli_category_id=base_listing.meli_category_id,
                        listing_type=base_listing.listing_type,
                        variation_asin=variation_asin,
                        status="draft",
                    )
                    db.add(variant_listing)

            # 2. Gather images: variant-specific first, fallback to product images on filesystem
            pictures = variation.images[:10] if variation.images else []
            if not pictures:
                pictures = fallback_pictures

            if not pictures:
                user_error = "Sin imágenes disponibles para esta variante"
                raise ValueError(user_error)

            # 3. Publish as single item (no ML variations array).
            # When family_name is required by the category, ML uses it as the visible title
            # and IGNORES the title field entirely. So for each variant we must incorporate
            # the dim_label into family_name so each listing has a distinct visible name.
            variant_family_name: Optional[str] = None
            if data.family_name:
                base_fn = data.family_name.strip()
                candidate_fn = f"{dim_label} - {base_fn}"
                # ML family_name limit: 60 chars (same as title)
                variant_family_name = candidate_fn[:60]

            # Merge base category attributes with per-variant ML attributes.
            # Per-variant attrs (COLOR, SIZE, FLAVOR…) override any matching base attr.
            merged_attributes: Optional[list] = extra_attributes
            if variation.ml_attributes:
                # Build a dict keyed by attr id for easy override
                base_map: dict = {}
                for a in (extra_attributes or []):
                    base_map[a["id"]] = a
                for ml_attr in variation.ml_attributes:
                    override: dict = {"id": ml_attr.id}
                    if ml_attr.value_id:
                        override["value_id"] = ml_attr.value_id
                    if ml_attr.value_name:
                        override["value_name"] = ml_attr.value_name
                    base_map[ml_attr.id] = override
                merged_attributes = list(base_map.values()) or None

            result = await publish_item(
                db=db,
                user_id=current_user.id,
                title=variant_listing.title,
                category_id=variant_listing.meli_category_id,
                price=float(variant_listing.meli_price),
                available_quantity=variant_listing.available_quantity,
                listing_type_id=variant_listing.listing_type,
                condition=data.condition or "new",
                description=variant_listing.description or "",
                pictures=pictures,
                brand=brand,
                model=variant_title,
                extra_attributes=merged_attributes,
                family_name=variant_family_name,
                sale_terms=sale_terms,
                shipping=shipping,
                variations=None,
            )

            if result is None or result.get("error"):
                raw = (result or {})
                # Extract the most user-friendly message from ML error
                ml_msg = raw.get("message") or raw.get("detail") or ""
                causes = raw.get("cause", [])
                cause_msgs = [c.get("message", "") for c in causes if c.get("message")]
                if cause_msgs:
                    user_error = "; ".join(cause_msgs)
                elif ml_msg:
                    user_error = ml_msg
                else:
                    user_error = "Error al publicar en Mercado Libre"
                raise ValueError(user_error)

            # 4. Update variant listing with ML data
            async with db.begin_nested():
                variant_listing.meli_item_id = result.get("id")
                variant_listing.meli_permalink = result.get("permalink")
                variant_listing.status = "active"
            await db.commit()

            results.append(ListingVariantResult(
                variation_asin=variation_asin,
                variant_name=dim_label,
                success=True,
                listing_id=variant_listing.id,
                meli_item_id=variant_listing.meli_item_id,
                permalink=variant_listing.meli_permalink,
            ))
            logger.info(f"[publish-variants] Published variant {variation_asin} → {variant_listing.meli_item_id}")

        except Exception as exc:
            logger.error(f"[publish-variants] Failed variant {variation_asin}: {exc}")
            try:
                await db.rollback()
            except Exception:
                pass
            results.append(ListingVariantResult(
                variation_asin=variation_asin,
                variant_name=dim_label,
                success=False,
                error=user_error or _friendly_error(exc),
            ))

    succeeded = sum(1 for r in results if r.success)

    return MeliPublishBulkResponse(
        results=results,
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
    )
