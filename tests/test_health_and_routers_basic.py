"""Integration tests for health endpoints and basic router auth checks."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.config import settings
from app.dependencies import get_db
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_token(user_id: int = 1, is_superuser: bool = False, role: str = "user") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    payload = {
        "sub": str(user_id),
        "is_superuser": is_superuser,
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def make_mock_db(fail: bool = False):
    session = AsyncMock()
    if fail:
        session.execute = AsyncMock(side_effect=Exception("DB unavailable"))
    else:
        session.execute = AsyncMock(return_value=MagicMock())
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client_ok():
    async def override_get_db():
        yield make_mock_db(fail=False)

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def client_db_fail():
    async def override_get_db():
        yield make_mock_db(fail=True)

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Health + root
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_root_returns_running(client_ok):
    response = await client_ok.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["name"] == "Meli-API"


@pytest.mark.asyncio
async def test_health_check_ok(client_ok):
    response = await client_ok.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["db"] is True


@pytest.mark.asyncio
async def test_health_check_db_failure_returns_503(client_db_fail):
    response = await client_db_fail.get("/health")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_docs_available(client_ok):
    response = await client_ok.get("/docs")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Protected endpoints — no auth -> 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listings_without_auth_returns_401(client_ok):
    response = await client_ok.get("/listings")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_orders_without_auth_returns_401(client_ok):
    response = await client_ok.get("/orders")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_meli_status_without_auth_returns_401(client_ok):
    response = await client_ok.get("/meli/status")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Internal API key endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listings_stats_wrong_api_key_returns_401_or_403(client_ok):
    # /listings/stats uses verify_api_key — wrong key → 403
    # but FastAPI may also return 401 if the header format is unexpected
    response = await client_ok.get(
        "/listings/stats", headers={"x-api-key": "wrong-key"}
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_listings_stats_invalid_api_key_blocked(client_ok):
    # Wrong API key must NOT pass through (403 or 401)
    response = await client_ok.get(
        "/listings/stats",
        headers={"x-api-key": "definitely-wrong-key"},
        params={"user_id": 1},
    )
    assert response.status_code in (401, 403)
