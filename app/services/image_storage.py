"""Local filesystem image storage for product images."""

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/app/media"))
PRODUCTS_DIR = MEDIA_ROOT / "products"


def _product_dir(product_id: int) -> Path:
    """Return the directory for a product's images."""
    return PRODUCTS_DIR / str(product_id)


async def save_images(product_id: int, files: list[tuple[str, bytes]]) -> list[str]:
    """
    Save image files to disk.

    Args:
        product_id: The product ID to associate images with.
        files: List of (filename, content_bytes) tuples.

    Returns:
        List of relative paths (e.g. "products/123/image_0.jpg").
    """
    dest = _product_dir(product_id)
    dest.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for i, (filename, content) in enumerate(files):
        ext = Path(filename).suffix or ".jpg"
        safe_name = f"image_{i}{ext}"
        filepath = dest / safe_name
        filepath.write_bytes(content)
        saved.append(f"products/{product_id}/{safe_name}")
        logger.info(f"Saved image {safe_name} for product {product_id} ({len(content)} bytes)")

    return saved


def get_image_paths(product_id: int) -> list[str]:
    """
    Get all image file paths for a product.

    Returns:
        List of absolute file paths sorted by name.
    """
    dest = _product_dir(product_id)
    if not dest.exists():
        return []
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    paths = sorted(
        str(p) for p in dest.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )
    return paths


def get_image_urls(product_id: int, base_url: str = "") -> list[str]:
    """
    Get serveable URLs for a product's images.

    Args:
        product_id: The product ID.
        base_url: Base URL prefix (e.g. "https://local.api.milisps.dropshopingsps.com").

    Returns:
        List of URLs like "{base_url}/media/products/123/image_0.jpg".
    """
    dest = _product_dir(product_id)
    if not dest.exists():
        return []
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    urls = sorted(
        f"{base_url}/media/products/{product_id}/{p.name}"
        for p in dest.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )
    return urls


def delete_images(product_id: int) -> None:
    """Delete all images for a product."""
    dest = _product_dir(product_id)
    if dest.exists():
        shutil.rmtree(dest)
        logger.info(f"Deleted images for product {product_id}")
