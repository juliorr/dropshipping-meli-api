"""Tests for app/dependencies.py — JWT auth and API key verification."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app.config import settings
from app.dependencies import AuthUser, get_current_user, verify_api_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_token(
    user_id: int = 1,
    is_superuser: bool = False,
    token_type: str = "access",
    expires_delta: timedelta = timedelta(minutes=30),
    secret: str | None = None,
) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": str(user_id),
        "is_superuser": is_superuser,
        "type": token_type,
        "exp": expire,
    }
    key = secret or settings.jwt_secret
    return jwt.encode(payload, key, algorithm=settings.jwt_algorithm)


def make_request(cookie_token: str | None = None) -> MagicMock:
    req = MagicMock()
    req.cookies = {}
    if cookie_token:
        req.cookies["access_token"] = cookie_token
    return req


def make_credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ---------------------------------------------------------------------------
# get_current_user — valid tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_valid_token():
    token = make_token(user_id=42, is_superuser=True)
    user = await get_current_user(make_request(), make_credentials(token))

    assert isinstance(user, AuthUser)
    assert user.id == 42
    assert user.is_superuser is True


@pytest.mark.asyncio
async def test_get_current_user_valid_non_superuser():
    token = make_token(user_id=7, is_superuser=False)
    user = await get_current_user(make_request(), make_credentials(token))

    assert user.id == 7
    assert user.is_superuser is False


@pytest.mark.asyncio
async def test_get_current_user_from_cookie():
    token = make_token(user_id=5)
    user = await get_current_user(make_request(cookie_token=token), None)

    assert user.id == 5


# ---------------------------------------------------------------------------
# get_current_user — invalid tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(make_request(), make_credentials("this.is.not.valid"))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_expired_token():
    token = make_token(expires_delta=timedelta(seconds=-10))
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(make_request(), make_credentials(token))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_wrong_token_type_refresh():
    token = make_token(token_type="refresh")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(make_request(), make_credentials(token))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_wrong_secret():
    token = make_token(secret="totally_wrong_secret_xyz")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(make_request(), make_credentials(token))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_no_header_no_cookie():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(make_request(), None)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# verify_api_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_api_key_valid():
    # Should not raise
    await verify_api_key(x_api_key=settings.meli_api_key)


@pytest.mark.asyncio
async def test_verify_api_key_invalid():
    with pytest.raises(HTTPException) as exc_info:
        await verify_api_key(x_api_key="wrong-key-xyz")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_api_key_empty_string():
    with pytest.raises(HTTPException) as exc_info:
        await verify_api_key(x_api_key="")
    assert exc_info.value.status_code == 403
