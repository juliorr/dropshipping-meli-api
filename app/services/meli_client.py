"""Mercado Libre API client - HTTP operations for ML API."""

import asyncio
import io
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.meli_auth import get_valid_token

# ── SSRF protection for image downloads ──────────────────────────────────────
# Allowed base directory for local file reads
_ALLOWED_LOCAL_BASE = Path("/app/media")

# Docker internal service hostnames to block
_BLOCKED_HOSTNAMES = frozenset({
    "redis", "meli-redis", "postgres", "meli-postgres",
    "backend", "frontend", "meli-api", "flower", "nginx",
    "localhost", "host.docker.internal",
})


def _is_safe_url(url: str) -> bool:
    """Check that a URL does not point to internal/private resources."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    if hostname in _BLOCKED_HOSTNAMES:
        return False
    # Block private/reserved IPs (cloud metadata, internal networks)
    try:
        addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for addr_info in addrs:
            ip = ipaddress.ip_address(addr_info[4][0])
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                return False
    except (socket.gaierror, ValueError, OSError):
        pass  # DNS resolution failed — allow httpx to handle the error
    return True

# ── Retry configuration ───────────────────────────────────────────────────────
# Maximum number of retry attempts for transient ML API errors (5xx, timeouts)
_ML_MAX_RETRIES = 3
# Base delay in seconds for exponential back-off: 2s, 4s, 8s
_ML_RETRY_BASE_DELAY = 2.0
# HTTP status codes that indicate a transient error worth retrying
_ML_RETRYABLE_STATUSES = {500, 502, 503, 504}

logger = logging.getLogger(__name__)

MELI_API_BASE = "https://api.mercadolibre.com"

# User-Agent to use when downloading images from Amazon (avoids hotlink blocking)
_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _meli_request(
    db: AsyncSession,
    user_id: int,
    method: str,
    endpoint: str,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """Make an authenticated request to the ML API."""
    access_token = await get_valid_token(db, user_id)
    if not access_token:
        logger.error(f"No valid ML token for user {user_id}")
        return None

    url = f"{MELI_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, _ML_MAX_RETRIES + 1):
      try:
        # ── Log as curl command for easy debugging ──
        import json as _json
        if json_data is not None:
            # Show full payload but replace picture ids/sources with summary
            _log_data = dict(json_data)
            if "pictures" in _log_data and isinstance(_log_data["pictures"], list):
                _log_data["pictures"] = f"[{len(_log_data['pictures'])} pictures omitted]"
            _json_str = _json.dumps(_log_data, ensure_ascii=False, indent=2)
            _redacted = f"***{access_token[-4:]}" if len(access_token) >= 4 else "***"
            logger.warning(
                f"[ML API CURL] curl -X {method} '{url}' \\\n"
                f"  -H 'Authorization: Bearer {_redacted}' \\\n"
                f"  -H 'Content-Type: application/json' \\\n"
                f"  -d '{_json_str}'"
            )
        else:
            _params_str = f"?{'&'.join(f'{k}={v}' for k,v in (params or {}).items())}" if params else ""
            _redacted = f"***{access_token[-4:]}" if len(access_token) >= 4 else "***"
            logger.warning(f"[ML API CURL] curl -X {method} '{url}{_params_str}' -H 'Authorization: Bearer {_redacted}'")

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=15.0,
            )

        if response.status_code in (200, 201):
            return response.json()

        # ── Transient errors: retry with exponential back-off ──────────────
        if response.status_code in _ML_RETRYABLE_STATUSES and attempt < _ML_MAX_RETRIES:
            delay = _ML_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                f"[ML API] {method} {endpoint} → {response.status_code} "
                f"(attempt {attempt}/{_ML_MAX_RETRIES}), retrying in {delay:.0f}s..."
            )
            await asyncio.sleep(delay)
            continue

        # ── Non-retryable error or final attempt ───────────────────────────
        try:
            err_body = response.json()
            ml_message = err_body.get("message", "")
            ml_error   = err_body.get("error", "")
            ml_cause   = err_body.get("cause", [])
            cause_str  = ""
            if ml_cause:
                cause_str = " | causes: " + "; ".join(
                    f"[{c.get('type','?')}] {c.get('code','?')}: {c.get('message','')}"
                    for c in ml_cause
                )
        except Exception:
            ml_message = response.text
            ml_error   = ""
            cause_str  = ""

        payload_summary = ""
        if json_data:
            safe = {k: v for k, v in json_data.items() if k not in ("pictures", "description")}
            payload_summary = f"\n  payload : {safe}"

        log_msg = (
            f"[ML API {'WARNING' if response.status_code < 500 else 'ERROR'}] "
            f"{method} {endpoint} → {response.status_code}\n"
            f"  message : {ml_message}\n"
            f"  error   : {ml_error}"
            f"{cause_str}"
            f"{payload_summary}"
        )

        if response.status_code >= 500:
            logger.error(log_msg)
        else:
            logger.warning(log_msg)

        logger.warning(f"[ML API FULL RESPONSE] {response.text[:2000]}")
        return {"error": True, "status": response.status_code, "detail": response.text}

      except httpx.TimeoutException as e:
        if attempt < _ML_MAX_RETRIES:
            delay = _ML_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                f"[ML API] {method} {endpoint} timeout "
                f"(attempt {attempt}/{_ML_MAX_RETRIES}), retrying in {delay:.0f}s..."
            )
            await asyncio.sleep(delay)
        else:
            logger.error(f"[ML API] {method} {endpoint} timed out after {_ML_MAX_RETRIES} attempts: {e}")
            return None

      except Exception as e:
        logger.error(f"ML API request error: {e}")
        return None

    return None  # All retries exhausted


async def _download_image(url: str) -> Optional[bytes]:
    """
    Download an image from a URL (e.g. Amazon CDN) or read from local filesystem path.
    Returns raw bytes or None on failure.
    """
    # Local filesystem path — restricted to /app/media/ to prevent path traversal
    if url.startswith("/"):
        try:
            path = Path(url).resolve()
            if not path.is_relative_to(_ALLOWED_LOCAL_BASE):
                logger.warning(f"Local path outside allowed directory, blocked: {url}")
                return None
            if not path.is_file():
                logger.warning(f"Local image file not found: {url}")
                return None
            image_bytes = path.read_bytes()
            if len(image_bytes) < 1000:
                logger.warning(f"Image too small ({len(image_bytes)} bytes), skipping: {url}")
                return None
            logger.info(f"Read local image: {url} ({len(image_bytes)} bytes)")
            return image_bytes
        except Exception as e:
            logger.error(f"Error reading local image {url}: {e}")
            return None

    # Block SSRF: prevent requests to internal services and private IPs
    if not _is_safe_url(url):
        logger.warning(f"Blocked SSRF attempt in image download: {url}")
        return None

    headers = {
        "User-Agent": _DOWNLOAD_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.amazon.com/",
    }
    # Max image size we accept: 20 MB. Bail out early to protect memory.
    _MAX_IMAGE_BYTES = 20 * 1024 * 1024
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers, timeout=30.0) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        f"Image download failed: status={resp.status_code}, url={url}"
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        logger.warning(
                            f"Image exceeds {_MAX_IMAGE_BYTES // (1024*1024)} MB limit, "
                            f"aborting download: {url}"
                        )
                        return None
                    chunks.append(chunk)
                image_bytes = b"".join(chunks)
                if len(image_bytes) < 1000:
                    logger.warning(
                        f"Image too small ({len(image_bytes)} bytes), skipping: {url}"
                    )
                    return None
                return image_bytes
    except Exception as e:
        logger.error(f"Image download error for {url}: {e}")
        return None


# Minimum pixel size ML requires on at least one side (after removing white borders)
# We target a comfortable 800px to give ML room after its 10% border-removal processing
_ML_MIN_SIDE_PX = 500
_ML_TARGET_SIDE_PX = 800


def _ensure_min_size(image_bytes: bytes) -> bytes:
    """
    Ensure an image meets MercadoLibre's minimum size requirement of 500px on at least one side.
    ML removes up to 10% white borders, so we target 800px to be safe.

    Returns the (possibly resized) image as JPEG bytes.
    If the image already meets the requirement, returns the original bytes unchanged.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        orig_w, orig_h = img.size
        min_side = min(orig_w, orig_h)

        if min_side >= _ML_TARGET_SIDE_PX:
            # Already big enough — just return original bytes
            return image_bytes

        # Scale so the shortest side reaches the target
        scale = _ML_TARGET_SIDE_PX / min_side
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        logger.info(
            f"Resizing image from {orig_w}x{orig_h} → {new_w}x{new_h} "
            f"(min side was {min_side}px, target {_ML_TARGET_SIDE_PX}px)"
        )

        # Use LANCZOS resampling for best quality when upscaling
        img = img.convert("RGB")  # Ensure no alpha channel for JPEG
        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img_resized.save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    except Exception as e:
        logger.warning(f"Could not resize image: {e} — using original bytes")
        return image_bytes


async def upload_picture_to_meli(
    db: AsyncSession,
    user_id: int,
    image_url: str,
) -> Optional[str]:
    """
    Download an image from a URL and upload it to MercadoLibre's picture hosting.
    Returns the ML picture ID (e.g. '841557-MLA123456789_012025') or None on failure.

    This avoids the problem where ML cannot fetch images from Amazon's CDN
    (hotlink protection / 403 errors) by uploading the binary directly.

    Also resizes images that are too small (<800px on the shortest side) so they
    meet ML's minimum requirement of 500px on at least one side (after white-border removal).
    """
    # Step 1: Download the image from the source (Amazon)
    image_bytes = await _download_image(image_url)
    if not image_bytes:
        logger.warning(f"Could not download image, falling back to source URL: {image_url}")
        return None

    # Step 2: Ensure image meets ML minimum size (resize if too small)
    image_bytes = _ensure_min_size(image_bytes)

    # Step 3: Upload to ML picture hosting
    access_token = await get_valid_token(db, user_id)
    if not access_token:
        logger.error(f"No valid ML token for user {user_id}")
        return None

    upload_url = f"{MELI_API_BASE}/pictures/items/upload"
    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                upload_url,
                headers=headers,
                # Always upload as JPEG after potential resize/conversion
                files={"file": ("image.jpg", image_bytes, "image/jpeg")},
                timeout=30.0,
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            picture_id = data.get("id")
            logger.info(f"Image uploaded to ML: {picture_id} (from {image_url[:80]}...)")
            return picture_id
        else:
            logger.error(f"ML picture upload failed: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        logger.error(f"ML picture upload error: {e}")
        return None


async def upload_pictures_to_meli(
    db: AsyncSession,
    user_id: int,
    image_urls: List[str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Upload multiple images to ML. Returns a tuple of:
    - List of picture dicts for the items API (either {"id": "..."} or {"source": "..."} as fallback)
    - List of warning messages for any failed uploads

    Uploads are done concurrently for speed.
    """
    if not image_urls:
        return [], []

    warnings = []

    async def _upload_one(url: str) -> Dict[str, str]:
        pic_id = await upload_picture_to_meli(db, user_id, url)
        if pic_id:
            return {"id": pic_id}
        else:
            warnings.append(f"Could not upload image, using source URL as fallback: {url[:80]}")
            return {"source": url}

    # Upload all images concurrently (max 10)
    tasks = [_upload_one(url) for url in image_urls[:10]]
    picture_entries = await asyncio.gather(*tasks)

    return list(picture_entries), warnings


def _sanitize_attribute_value(attr_id: str, value: str) -> Optional[str]:
    """
    Sanitize attribute values before sending to ML API.

    Rules:
    - WEIGHT: If value is a bare number without unit, append " g" (ML requires unit).
      Examples: "200" → "200 g", "0.5" → "0.5 g", "200 g" → unchanged.
    - GTIN: Validate EAN-13 / EAN-8 / UPC-A checksum. Return None to omit if invalid.
    - All others: return unchanged.
    """
    import re as _re

    if attr_id == "WEIGHT" and value:
        # If it's a pure number (int or float), add "g" unit
        if _re.match(r"^\d+(\.\d+)?$", value.strip()):
            return f"{value.strip()} g"
        return value

    if attr_id == "GTIN" and value:
        digits = _re.sub(r"\D", "", value)

        # ── Padding de ceros a la izquierda ──────────────────────────────────
        # Los formatos GTIN válidos son 8, 12, 13 o 14 dígitos.
        # Si el valor extraído tiene menos caracteres que el formato mínimo más
        # cercano, se rellena con ceros por la izquierda para intentar que sea
        # un código válido antes de enviarlo a ML.
        #   1–8  dígitos → rellenar a 8  (EAN-8)
        #   9–12 dígitos → rellenar a 12 (UPC-A)
        #   13   dígitos → rellenar a 13 (EAN-13)
        #   14+  dígitos → rellenar a 14 (GTIN-14), truncar si excede
        if len(digits) > 0:
            if len(digits) <= 8:
                digits = digits.zfill(8)
            elif len(digits) <= 12:
                digits = digits.zfill(12)
            elif len(digits) == 13:
                pass  # ya es EAN-13
            else:
                digits = digits[:14]  # truncar a 14 si excede

            if len(digits) != len(_re.sub(r"\D", "", value)):
                logger.info(
                    f"GTIN paddeado: '{value}' → '{digits}' "
                    f"({len(_re.sub(r'\\D', '', value))} → {len(digits)} dígitos)"
                )

        if len(digits) not in (8, 12, 13, 14):
            logger.warning(f"GTIN '{value}' tiene longitud inválida ({len(digits)} dígitos) — se envía igual para que ML lo valide.")
            return digits if digits else value
        # Validate Luhn-like checksum used by GS1 (EAN/UPC)
        total = 0
        for i, d in enumerate(reversed(digits[:-1])):
            n = int(d)
            total += n * (3 if i % 2 == 0 else 1)
        check = (10 - (total % 10)) % 10
        if check != int(digits[-1]):
            logger.warning(f"GTIN '{digits}' tiene checksum inválido — se omite para evitar error 400 en ML.")
            return None  # omitir atributo; ML rechaza GTINs con checksum incorrecto con error 400
        return digits

    return value


async def publish_item(
    db: AsyncSession,
    user_id: int,
    title: str,
    category_id: str,
    price: float,
    currency_id: str = "MXN",
    available_quantity: int = 1,
    buying_mode: str = "buy_it_now",
    listing_type_id: str = "gold_special",
    condition: str = "new",
    description: str = "",
    pictures: Optional[List[str]] = None,
    brand: Optional[str] = None,
    model: Optional[str] = None,
    extra_attributes: Optional[List[Dict[str, Any]]] = None,
    family_name: Optional[str] = None,
    sale_terms: Optional[List[Dict[str, Any]]] = None,
    shipping: Optional[Dict[str, Any]] = None,
    variations: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Publish a new item on Mercado Libre.
    Returns the created item data including meli_item_id and permalink.
    Supports dynamic attributes from category requirements.

    Images are downloaded from their source (e.g. Amazon) and uploaded directly
    to ML's picture hosting to avoid hotlink blocking issues.

    NOTE: Description is published in a separate POST /items/{id}/description request
    after the item is created, as required by the ML API specification.

    extra_attributes: list of dicts like [{"id": "BRAND", "value_name": "Sony"}, ...]
                      Each dict can have 'id' + 'value_name' (free text) or 'id' + 'value_id' (catalog).

    family_name: Optional. When provided, the item is published in "family mode" (ML product family).
                 In family mode, 'title' must NOT be sent (ML rejects it with "body.invalid_fields").
                 The family_name is enriched with brand/model if too short to pass ML validation.
                 Required by some generic categories (e.g. "Otros").
    """
    # ---------------------------------------------------------------------------
    # Brand validation helpers — defined early so they apply to BOTH family_name
    # AND the BRAND attribute (avoiding invalid values like "Amazon's Choice").
    # ---------------------------------------------------------------------------
    _AMAZON_STORE_KEYWORDS = (
        "store", "shop", "brand", "seller", "official", "tienda", "marca",
        # Amazon badge/label strings that appear as product "brand" in scraped data
        "amazon's choice", "amazons choice", "overall pick", "climate pledge",
        "best seller", "bestseller", "limited deal", "prime", "sponsored",
    )
    _GENERIC_BRAND_NAMES = (
        "genérica", "generica", "generic", "sin marca", "no brand", "n/a", "none"
    )

    def _is_real_brand(b: Optional[str]) -> bool:
        if not b:
            return False
        bl = b.lower().strip()
        if bl in _GENERIC_BRAND_NAMES:
            return False
        # Amazon seller store names and badge labels
        if any(kw in bl for kw in _AMAZON_STORE_KEYWORDS):
            return False
        return True

    # Resolve the effective brand name: None if it's not a real brand, so callers
    # use "Genérica" as fallback in attributes and title[:60] for family_name.
    effective_brand = brand if _is_real_brand(brand) else None

    # Build attributes list from dynamic attributes or fallback to brand/model
    attributes = []
    sent_attr_ids = set()

    # First, add any dynamic extra_attributes (these take priority)
    # Detect if GTIN is present with a real value — if so, EMPTY_GTIN_REASON must be excluded
    # (they are mutually exclusive: sending both confuses the ML API).
    _gtin_has_value = any(
        a.get("id") == "GTIN" and (a.get("value_id") or a.get("value_name", "").strip())
        for a in (extra_attributes or [])
    )
    # Track if a GTIN was present but got omitted due to invalid value (bad checksum/length)
    _gtin_was_omitted = False
    if extra_attributes:
        for attr in extra_attributes:
            attr_id = attr["id"]
            # Skip EMPTY_GTIN_REASON when a real GTIN value is present
            if attr_id == "EMPTY_GTIN_REASON" and _gtin_has_value:
                logger.info("Skipping EMPTY_GTIN_REASON because GTIN attribute has a value.")
                continue
            attr_entry = {"id": attr_id}
            if attr.get("value_id"):
                # Use value_id (catalog value) — do NOT also send value_name,
                # as sending both causes ML validation errors.
                attr_entry["value_id"] = attr["value_id"]
            elif attr.get("value_name"):
                value = str(attr["value_name"])
                # Sanitize BRAND attribute: if the value is an Amazon badge/label
                # (e.g. "Amazon's Choice: Overall Pick"), replace with the validated
                # effective_brand or "Genérica" to avoid ML 400 errors.
                if attr_id == "BRAND" and not _is_real_brand(value):
                    value = effective_brand or "Genérica"
                else:
                    # Apply generic attribute sanitization (WEIGHT units, GTIN checksum, etc.)
                    sanitized = _sanitize_attribute_value(attr_id, value)
                    if sanitized is None:
                        # Skip this attribute — invalid value (e.g. bad GTIN)
                        logger.warning(f"Omitting attribute {attr_id} with invalid value: {value!r}")
                        if attr_id == "GTIN":
                            _gtin_was_omitted = True
                        continue
                    value = sanitized
                attr_entry["value_name"] = value
            attributes.append(attr_entry)
            sent_attr_ids.add(attr_id)

    # Si el GTIN fue omitido por ser inválido (checksum/longitud incorrectos) y no hay ya un
    # EMPTY_GTIN_REASON en los atributos, inyectarlo automáticamente para que ML no rechace
    # la publicación por falta de código de barras.
    if _gtin_was_omitted and "EMPTY_GTIN_REASON" not in sent_attr_ids:
        logger.info(
            "GTIN omitido por valor inválido — auto-inyectando "
            "EMPTY_GTIN_REASON='El producto no tiene código registrado'."
        )
        attributes.append({"id": "EMPTY_GTIN_REASON", "value_name": "El producto no tiene código registrado"})
        sent_attr_ids.add("EMPTY_GTIN_REASON")

    # Fallback: add BRAND if not already in extra_attributes.
    # Use effective_brand (validated) to avoid sending Amazon badge labels as brand.
    if "BRAND" not in sent_attr_ids:
        if effective_brand:
            attributes.append({"id": "BRAND", "value_name": effective_brand})
        else:
            attributes.append({"id": "BRAND", "value_name": "Genérica"})

    # Fallback: add MODEL if not already in extra_attributes
    if "MODEL" not in sent_attr_ids and model:
        attributes.append({"id": "MODEL", "value_name": model})

    # Upload images to ML first (avoids Amazon CDN hotlink blocking)
    picture_entries = []
    upload_warnings = []
    if pictures:
        logger.info(f"Uploading {len(pictures)} images to ML picture hosting...")
        picture_entries, upload_warnings = await upload_pictures_to_meli(db, user_id, pictures)
        logger.info(f"Image upload complete: {len(picture_entries)} entries, {len(upload_warnings)} warnings")

        # Check if at least one image was successfully hosted on ML (has an "id" key).
        # If all entries are source-URL fallbacks ({"source": "..."}), ML may still reject
        # them (e.g. hotlink protection). Abort early with a clear error to avoid a
        # confusing "title invalid" response from the ML API.
        valid_picture_ids = [e for e in picture_entries if e.get("id")]
        if not valid_picture_ids:
            logger.error(
                f"All {len(pictures)} image(s) failed to upload to ML hosting for user {user_id}. "
                "Aborting publish to avoid misleading ML API errors."
            )
            return {
                "error": True,
                "status": 400,
                "detail": (
                    '{"message":"No se pudo subir ninguna imagen a Mercado Libre. '
                    "Las imágenes del producto pueden ser demasiado pequeñas o no estar disponibles. "
                    'Intenta actualizar los datos del producto desde Amazon e inténtalo de nuevo.",'
                    '"error":"image_upload_failed","status":400,"cause":[]}'
                ),
            }

    # ── Validate description: reject scraper error messages ──────────────────
    # When the Amazon scraper fails (e.g. CAPTCHA, page blocked), the error
    # message can leak through as the product "description". Detect and discard
    # such values so we never publish garbage to MercadoLibre.
    _DESCRIPTION_ERROR_PATTERNS = (
        "page.goto:",
        "página bloqueada",
        "error scraping",
        "navigation error",
        "timeout",
        "net::err_",
        "registro de llamadas",
        "domcontentloaded",
        "waituntil",
    )
    if description:
        desc_lower = description.lower()
        if any(pattern in desc_lower for pattern in _DESCRIPTION_ERROR_PATTERNS):
            logger.warning(
                f"Description rejected — looks like a scraper error message: {description[:120]!r}"
            )
            description = ""

    # ML API does NOT accept 'description' in the item creation payload.
    # It must be sent in a separate POST /items/{id}/description request after creation.
    #
    # FAMILY MODE: When family_name is provided, ML treats the listing as part of a product
    # family (variantes). In this mode, 'title' must NOT be sent — ML rejects it with
    # "The fields [title] are invalid for requested call." Only family_name is used instead.
    # However, ML still validates family_name similarly to title (minimum_length, must include
    # brand/model info), so we enrich it the same way we enrich titles.
    # This is required by some generic categories (e.g. "Otros Accesorios para Pesca").
    # ---------------------------------------------------------------------------
    # Enriquecer el título si es demasiado corto/genérico.
    # ML exige que el título incluya características como marca, modelo o categoría.
    # Si el título tiene < 20 caracteres, se prepende la marca efectiva y/o modelo
    # para que sea más descriptivo y pase la validación de minimum_length.
    # ---------------------------------------------------------------------------
    enriched_title = title.strip()
    if len(enriched_title) < 20:
        parts = []
        if effective_brand and effective_brand != "Genérica":
            parts.append(effective_brand)
        if model and model != enriched_title:
            parts.append(model)
        if parts:
            candidate = " ".join(parts) + " " + enriched_title
            enriched_title = candidate.strip()
            logger.info(
                f"Título enriquecido: {title!r} ({len(title)} chars) → {enriched_title!r} ({len(enriched_title)} chars)"
            )
    # Truncar al límite de ML (60 chars)
    enriched_title = enriched_title[:60]

    # ── Build ML variations array ────────────────────────────────────────────
    # Maps Amazon dimension key names to ML attribute IDs.
    # Fallback: strip "_name" suffix and uppercase (e.g. "size_name" → "SIZE").
    _VARIATION_ATTR_MAP: Dict[str, str] = {
        "size_name": "SIZE",
        "color_name": "COLOR",
        "style_name": "STYLE",
        "flavor_name": "FLAVOR",
        "material_type": "MATERIAL_TYPE",
        "scent_name": "SCENT_NAME",
        "item_package_quantity": "ITEM_PACKAGE_QUANTITY",
        "pattern_name": "PATTERN",
        "configuration_name": "CONFIGURATION",
    }

    # ML does NOT allow variations together with family_name — they are mutually exclusive.
    ml_variations: Optional[list] = None
    if variations and family_name:
        logger.warning(
            f"[Variations] family_name='{family_name}' was provided together with "
            f"{len(variations)} variation(s). ML does not support both simultaneously — "
            "variations will be IGNORED and the item will publish as a single listing."
        )
    if variations and not family_name:
        logger.info(f"Building ML variations array for {len(variations)} variants...")
        ml_variations = []
        for var in variations:
            var_pictures_raw = var.get("pictures", [])
            var_price = float(var.get("price", price))
            var_qty = int(var.get("available_quantity", available_quantity))
            var_attrs = var.get("attributes", {})
            var_labels = var.get("display_labels", {})

            # Upload variant images
            var_picture_ids = []
            if var_pictures_raw:
                var_pic_entries, _ = await upload_pictures_to_meli(db, user_id, var_pictures_raw)
                var_picture_ids = [e["id"] for e in var_pic_entries if e.get("id")]

            # Build attribute_combinations for this variant
            attr_combinations = []
            for dim_key, dim_value in var_attrs.items():
                ml_attr_id = _VARIATION_ATTR_MAP.get(dim_key)
                if not ml_attr_id:
                    # Fallback: strip _name suffix, uppercase
                    ml_attr_id = dim_key.replace("_name", "").replace("_", " ").upper()
                attr_combinations.append({"id": ml_attr_id, "value_name": str(dim_value)})

            var_entry: Dict[str, Any] = {
                "price": var_price,
                "available_quantity": var_qty,
                "attribute_combinations": attr_combinations,
            }
            if var_picture_ids:
                var_entry["picture_ids"] = var_picture_ids
            ml_variations.append(var_entry)

        logger.info(f"Built {len(ml_variations)} ML variation entries.")

    if family_name:
        # Enrich family_name if too short/generic (same logic as title enrichment).
        # ML validates family_name with similar rules: must include brand, model, etc.
        enriched_family = family_name.strip()
        if len(enriched_family) < 20:
            parts = []
            if effective_brand and effective_brand != "Genérica":
                parts.append(effective_brand)
            if model and model != enriched_family:
                parts.append(model)
            if parts:
                candidate = " ".join(parts) + " " + enriched_family
                enriched_family = candidate.strip()
                logger.info(
                    f"family_name enriquecido: {family_name!r} ({len(family_name)} chars) "
                    f"→ {enriched_family!r} ({len(enriched_family)} chars)"
                )
        enriched_family = enriched_family[:60]

        logger.info(f"Publishing in family mode: family_name={enriched_family!r} (title omitted)")
        item_data = {
            "family_name": enriched_family,  # ML limit: 60 chars; NO title in family mode
            "category_id": category_id,
            "price": price,
            "currency_id": currency_id,
            "available_quantity": available_quantity,
            "buying_mode": buying_mode,
            "listing_type_id": listing_type_id,
            "condition": condition,
            "pictures": picture_entries if picture_entries else None,
            "attributes": attributes if attributes else None,
            "sale_terms": sale_terms if sale_terms else None,
            "shipping": shipping if shipping else None,
            "variations": ml_variations if ml_variations else None,
        }
    else:
        item_data = {
            "title": enriched_title,  # Already capped at 60 chars, enriched if needed
            "category_id": category_id,
            "price": price,
            "currency_id": currency_id,
            "available_quantity": available_quantity,
            "buying_mode": buying_mode,
            "listing_type_id": listing_type_id,
            "condition": condition,
            "pictures": picture_entries if picture_entries else None,
            "attributes": attributes if attributes else None,
            "sale_terms": sale_terms if sale_terms else None,
            "shipping": shipping if shipping else None,
            "variations": ml_variations if ml_variations else None,
        }

    # When variations are provided, ML derives price and available_quantity from them,
    # so we remove the top-level fields to avoid conflicts.
    if ml_variations:
        item_data.pop("price", None)
        item_data.pop("available_quantity", None)

    # Remove None values
    item_data = {k: v for k, v in item_data.items() if v is not None}

    logger.info(f"Publishing item to ML with {len(attributes)} attributes: {[a['id'] for a in attributes]}")
    result = await _meli_request(db, user_id, "POST", "/items", json_data=item_data)
    if result and not result.get("error"):
        item_id = result.get("id")
        logger.info(f"Item published on ML: {item_id} for user {user_id}")

        # Publish description in a separate request (ML API requirement)
        if description and item_id:
            desc_result = await _meli_request(
                db, user_id, "POST", f"/items/{item_id}/description",
                json_data={"plain_text": description},
            )
            if desc_result and desc_result.get("error"):
                logger.warning(f"Description upload failed for {item_id}: {desc_result.get('detail', '')[:200]}")
            else:
                logger.info(f"Description published for ML item {item_id}")

        # Attach upload warnings to the result so the router can surface them
        if upload_warnings:
            existing_warnings = result.get("warnings", [])
            if isinstance(existing_warnings, list):
                existing_warnings.extend(upload_warnings)
            else:
                existing_warnings = upload_warnings
            result["warnings"] = existing_warnings
    return result


async def publish_item_catalog(
    db: AsyncSession,
    user_id: int,
    catalog_product_id: str,
    price: float,
    currency_id: str = "MXN",
    available_quantity: int = 1,
    buying_mode: str = "buy_it_now",
    listing_type_id: str = "gold_special",
    condition: str = "new",
    category_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Publish an item in ML catalog mode (no custom title, uses catalog product).
    Requires a valid catalog_product_id from GET /products/search.
    category_id is required by the ML API when publishing in catalog mode.
    """
    item_data: Dict[str, Any] = {
        "catalog_product_id": catalog_product_id,
        "price": price,
        "currency_id": currency_id,
        "available_quantity": available_quantity,
        "buying_mode": buying_mode,
        "listing_type_id": listing_type_id,
        "condition": condition,
        "catalog_listing": True,
    }
    if category_id:
        item_data["category_id"] = category_id

    logger.info(f"Publishing catalog item to ML: catalog_product_id={catalog_product_id}")
    result = await _meli_request(db, user_id, "POST", "/items", json_data=item_data)
    if result and not result.get("error"):
        logger.info(f"Catalog item published on ML: {result.get('id')} for user {user_id}")
    return result


async def update_item(
    db: AsyncSession,
    user_id: int,
    meli_item_id: str,
    updates: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Update an existing ML item."""
    return await _meli_request(
        db, user_id, "PUT", f"/items/{meli_item_id}", json_data=updates
    )


async def close_item(
    db: AsyncSession, user_id: int, meli_item_id: str
) -> Optional[Dict[str, Any]]:
    """Close an ML listing permanently."""
    return await update_item(db, user_id, meli_item_id, {"status": "closed"})


async def update_price(
    db: AsyncSession, user_id: int, meli_item_id: str, new_price: float
) -> Optional[Dict[str, Any]]:
    """Update the price of an ML item."""
    return await update_item(db, user_id, meli_item_id, {"price": new_price})


async def update_stock(
    db: AsyncSession, user_id: int, meli_item_id: str, quantity: int
) -> Optional[Dict[str, Any]]:
    """Update the available quantity of an ML item."""
    return await update_item(db, user_id, meli_item_id, {"available_quantity": quantity})


async def get_item(
    db: AsyncSession, user_id: int, meli_item_id: str
) -> Optional[Dict[str, Any]]:
    """Get item details from ML."""
    return await _meli_request(db, user_id, "GET", f"/items/{meli_item_id}")


async def get_orders(
    db: AsyncSession,
    user_id: int,
    meli_seller_id: str,
    offset: int = 0,
    limit: int = 50,
) -> Optional[Dict[str, Any]]:
    """Get seller orders from ML."""
    return await _meli_request(
        db, user_id, "GET", f"/orders/search",
        params={"seller": meli_seller_id, "offset": offset, "limit": limit},
    )


async def get_user_info(
    db: AsyncSession, user_id: int
) -> Optional[Dict[str, Any]]:
    """Get the ML user info for the connected account."""
    return await _meli_request(db, user_id, "GET", "/users/me")

