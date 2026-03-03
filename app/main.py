"""Meli-API — MercadoLibre integration service."""

import asyncio
import logging

# Import all models upfront so SQLAlchemy can resolve relationship strings.
import app.models  # noqa: F401

from app.config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(levelname)s:     %(name)s - %(message)s",
)
# Silence noisy third-party loggers regardless of app log level
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.routers.mercadolibre import router as meli_router
from app.routers.listings import router as listings_router
from app.routers.orders import router as orders_router
from app.routers.images import router as images_router, media_router
from app.scheduler import scheduler, setup_scheduler

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Meli-API — MercadoLibre Integration",
    description="Servicio independiente para integración con MercadoLibre",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meli_router)
app.include_router(listings_router)
app.include_router(orders_router)
app.include_router(images_router)
app.include_router(media_router)


@app.get("/", tags=["Health"])
async def root():
    return {
        "name": "Meli-API",
        "version": "1.0.0",
        "status": "running",
    }


@app.get("/health", tags=["Health"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """Health check — verifies DB connectivity."""
    db_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error(f"DB health check failed: {e}")

    if not db_ok:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail={"db": db_ok},
        )

    return {"status": "ok", "db": True}


@app.on_event("startup")
async def startup_scheduler():
    """Start the background scheduler and sync categories if cache is empty."""
    setup_scheduler()
    scheduler.start()
    logger.info("[Startup] Background scheduler started")

    # Sync categories if cache is empty
    try:
        from app.services.meli_categories import get_categories_cache_status, sync_categories_to_cache

        status = await get_categories_cache_status("MLM")
        if not status.get("cached"):
            logger.info("[Startup] Categories cache empty — syncing in background...")
            asyncio.create_task(sync_categories_to_cache("MLM"))
        else:
            count = status.get("count", 0)
            ttl = status.get("ttl_seconds", 0)
            logger.info(f"[Startup] Categories cache OK — {count} categories, TTL {ttl}s")
    except Exception as e:
        logger.warning(f"[Startup] Could not check categories cache: {e}")


@app.on_event("shutdown")
async def shutdown_scheduler():
    """Stop the background scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("[Shutdown] Background scheduler stopped")
