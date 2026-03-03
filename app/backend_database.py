"""
Read/write connection to the backend database.

The meli-api needs access to the `products` and `users` tables that live in
the backend's PostgreSQL. This is a pragmatic coupling that will be removed
in a future phase when product-status updates are moved to an internal HTTP
call to the backend API instead of a direct DB write.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

backend_engine = create_async_engine(
    settings.backend_database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    pool_timeout=30,
)

backend_async_session = async_sessionmaker(
    backend_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class BackendBase(DeclarativeBase):
    """Base for models that live in the backend database."""
    pass
