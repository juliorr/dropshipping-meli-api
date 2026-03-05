"""Unit tests for app/services/meli_auth.py (no real DB or HTTP calls)."""

import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.meli_auth import (
    _generate_code_challenge,
    _generate_code_verifier,
    get_auth_url,
    get_valid_token,
    refresh_meli_token,
)
from app.config import settings


# ---------------------------------------------------------------------------
# _generate_code_verifier
# ---------------------------------------------------------------------------

def test_generate_code_verifier_length():
    verifier = _generate_code_verifier()
    assert 43 <= len(verifier) <= 128


def test_generate_code_verifier_url_safe_chars():
    verifier = _generate_code_verifier()
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", verifier), (
        f"Verifier contains non-URL-safe chars: {verifier}"
    )


def test_generate_code_verifier_is_unique():
    v1 = _generate_code_verifier()
    v2 = _generate_code_verifier()
    assert v1 != v2


# ---------------------------------------------------------------------------
# _generate_code_challenge
# ---------------------------------------------------------------------------

def test_generate_code_challenge_deterministic():
    verifier = "test_verifier_abc123"
    assert _generate_code_challenge(verifier) == _generate_code_challenge(verifier)


def test_generate_code_challenge_correct_s256():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert _generate_code_challenge(verifier) == expected


def test_generate_code_challenge_url_safe_no_padding():
    challenge = _generate_code_challenge(_generate_code_verifier())
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", challenge)
    assert "=" not in challenge


def test_generate_code_challenge_different_verifiers_differ():
    assert _generate_code_challenge("verifier_one") != _generate_code_challenge("verifier_two")


# ---------------------------------------------------------------------------
# get_auth_url
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_auth_url_contains_required_params():
    url = await get_auth_url(user_id=1)

    assert "response_type=code" in url
    assert f"client_id={settings.meli_client_id}" in url
    assert "redirect_uri=" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=1" in url


@pytest.mark.asyncio
async def test_get_auth_url_different_state_per_user():
    url1 = await get_auth_url(user_id=1)
    url2 = await get_auth_url(user_id=2)
    assert "state=1" in url1
    assert "state=2" in url2


@pytest.mark.asyncio
async def test_get_auth_url_stores_verifier_in_cache():
    from app.cache import cache
    user_id = 9999
    await get_auth_url(user_id=user_id)
    cached = await cache.get(f"meli_pkce:{user_id}")
    assert cached is not None
    assert len(cached) >= 43


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_valid_token_no_token_in_db():
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await get_valid_token(mock_db, user_id=1)
    assert result is None


@pytest.mark.asyncio
async def test_get_valid_token_returns_access_token_when_valid():
    mock_db = AsyncMock()
    mock_token = MagicMock()
    mock_token.access_token = "valid_access_token_xyz"
    mock_token.expires_at = datetime.now(timezone.utc) + timedelta(hours=5)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_token
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await get_valid_token(mock_db, user_id=1)
    assert result == "valid_access_token_xyz"


# ---------------------------------------------------------------------------
# refresh_meli_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_meli_token_no_token_in_db():
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await refresh_meli_token(mock_db, user_id=1)
    assert result is None


@pytest.mark.asyncio
async def test_refresh_meli_token_calls_ml_api_and_updates_db():
    mock_db = AsyncMock()
    mock_token = MagicMock()
    mock_token.refresh_token = "old_refresh_token"
    mock_token.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_token
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "access_token": "new_access_token_abc",
        "refresh_token": "new_refresh_token_def",
        "expires_in": 21600,
    }

    with patch("app.services.meli_auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_response)
        mock_client_class.return_value = mock_client

        result = await refresh_meli_token(mock_db, user_id=42)

    assert result is not None
    assert mock_token.access_token == "new_access_token_abc"
    assert mock_token.refresh_token == "new_refresh_token_def"
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_meli_token_http_error_returns_none():
    mock_db = AsyncMock()
    mock_token = MagicMock()
    mock_token.refresh_token = "some_token"
    mock_token.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_token
    mock_db.execute = AsyncMock(return_value=mock_result)

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "Unauthorized"

    with patch("app.services.meli_auth.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_response)
        mock_client_class.return_value = mock_client

        result = await refresh_meli_token(mock_db, user_id=42)

    assert result is None
