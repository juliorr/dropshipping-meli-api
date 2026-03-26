"""Shared fixtures for meli-api tests."""

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from jose import jwt

from app.config import settings
from app.dependencies import get_db
from app.main import app


def make_jwt(
    user_id: int = 1,
    is_superuser: bool = False,
    role: str = "user",
    token_type: str = "access",
    expires_delta: timedelta = timedelta(minutes=30),
) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": str(user_id),
        "is_superuser": is_superuser,
        "role": role,
        "type": token_type,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.fixture
def valid_jwt_token() -> str:
    return make_jwt(user_id=1, is_superuser=False, role="user")


@pytest.fixture
def superuser_jwt_token() -> str:
    return make_jwt(user_id=99, is_superuser=True, role="admin")


@pytest.fixture
def expired_jwt_token() -> str:
    return make_jwt(expires_delta=timedelta(seconds=-1))


@pytest.fixture
def valid_api_key() -> str:
    return settings.meli_api_key


@pytest.fixture
def mock_db_session():
    """AsyncMock SQLAlchemy session for unit tests (no real DB)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
async def client(mock_db_session) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client with DB dependency overridden to a mock."""

    async def override_get_db():
        yield mock_db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
