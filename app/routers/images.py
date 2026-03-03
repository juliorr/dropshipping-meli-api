"""Image upload and serving endpoints."""

import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from app.dependencies import get_current_user
from app.services.image_storage import (
    MEDIA_ROOT,
    save_images,
    get_image_urls,
    delete_images,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["Product Images"])

MAX_IMAGES = 15
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per image


@router.post("/{product_id}/images")
async def upload_product_images(
    product_id: int,
    files: List[UploadFile] = File(...),
    current_user=Depends(get_current_user),
):
    """
    Upload image files for a product. Replaces any existing images.
    Accepts multipart/form-data with multiple files.
    """
    if len(files) > MAX_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_IMAGES} images allowed, got {len(files)}",
        )

    # Delete existing images first (replace strategy)
    delete_images(product_id)

    file_data: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_SIZE // (1024*1024)}MB limit",
            )
        file_data.append((f.filename or "image.jpg", content))

    saved = await save_images(product_id, file_data)
    logger.info(f"Uploaded {len(saved)} images for product {product_id}")

    return {
        "product_id": product_id,
        "images": saved,
        "count": len(saved),
    }


@router.get("/{product_id}/images")
async def list_product_images(
    product_id: int,
    current_user=Depends(get_current_user),
):
    """List all images for a product."""
    urls = get_image_urls(product_id)
    return {
        "product_id": product_id,
        "images": urls,
        "count": len(urls),
    }


@router.delete("/{product_id}/images")
async def delete_product_images(
    product_id: int,
    current_user=Depends(get_current_user),
):
    """Delete all images for a product."""
    delete_images(product_id)
    return {"message": f"Images deleted for product {product_id}"}


# Static file serving for images
media_router = APIRouter(tags=["Media"])


@media_router.get("/media/products/{product_id}/{filename}")
async def serve_image(product_id: int, filename: str):
    """Serve a product image file."""
    filepath = MEDIA_ROOT / "products" / str(product_id) / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    # Security: ensure path doesn't escape media directory
    try:
        filepath.resolve().relative_to(MEDIA_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    return FileResponse(filepath)
