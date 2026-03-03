"""Mercado Libre OAuth 2.0 service - Token management per user with PKCE."""

import asyncio
import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import cache
from app.config import settings
from app.models.meli_token import MeliToken

logger = logging.getLogger(__name__)

MELI_AUTH_URL = "https://auth.mercadolibre.com.mx/authorization"
MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# PKCE code_verifier TTL in seconds (10 minutes)
PKCE_VERIFIER_TTL = 600


def _generate_code_verifier() -> str:
    """Generate a random code_verifier (43-128 chars, URL-safe)."""
    return secrets.token_urlsafe(64)[:128]


def _generate_code_challenge(code_verifier: str) -> str:
    """Generate a S256 code_challenge from the code_verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def get_auth_url(user_id: int) -> str:
    """
    Generate the ML OAuth authorization URL with PKCE challenge.

    NOTE: If you get 403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES when publishing,
    ensure in the ML Developer Portal (https://developers.mercadolibre.com.mx/devcenter):
    1. Your app has "write" scope enabled in Scopes/Permisos
    2. If app is in TEST mode, add your ML user as a test user
    3. The user re-authorizes after scope changes (disconnect + reconnect from Settings)
    """
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)

    # Store code_verifier in memory keyed by user_id
    await cache.set(f"meli_pkce:{user_id}", code_verifier, ex=PKCE_VERIFIER_TTL)

    return (
        f"{MELI_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={settings.meli_client_id}"
        f"&redirect_uri={settings.meli_redirect_uri}"
        f"&state={user_id}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )


async def exchange_code_for_tokens(
    db: AsyncSession, user_id: int, code: str
) -> Optional[MeliToken]:
    """
    Exchange an authorization code for access/refresh tokens (with PKCE).
    Retrieves code_verifier from Redis and sends it along.
    Stores tokens in the database linked to the user.
    """
    try:
        # Retrieve code_verifier from cache
        code_verifier = await cache.get(f"meli_pkce:{user_id}")
        if not code_verifier:
            logger.error(f"No PKCE code_verifier found for user {user_id}")
            return None

        # Delete used verifier
        await cache.delete(f"meli_pkce:{user_id}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                MELI_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": settings.meli_client_id,
                    "client_secret": settings.meli_client_secret,
                    "code": code,
                    "redirect_uri": settings.meli_redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.error(f"ML token exchange failed: {response.status_code} - {response.text}")
            return None

        data = response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in", 21600)  # Default 6 hours
        meli_user_id = str(data.get("user_id", ""))
        token_type = data.get("token_type", "Bearer")

        if not access_token or not refresh_token:
            logger.error("ML token response missing tokens")
            return None

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Upsert token (one ML account per user)
        result = await db.execute(
            select(MeliToken).where(MeliToken.user_id == user_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.access_token = access_token
            existing.refresh_token = refresh_token
            existing.token_type = token_type
            existing.expires_at = expires_at
            existing.meli_user_id = meli_user_id
            token_obj = existing
        else:
            token_obj = MeliToken(
                user_id=user_id,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=token_type,
                expires_at=expires_at,
                meli_user_id=meli_user_id,
            )
            db.add(token_obj)

        await db.commit()
        await db.refresh(token_obj)
        logger.info(f"ML tokens stored for user {user_id} (ML user: {meli_user_id})")
        return token_obj

    except Exception as e:
        logger.error(f"ML token exchange error: {e}")
        return None


_refresh_locks: dict[int, asyncio.Lock] = {}


async def refresh_meli_token(
    db: AsyncSession, user_id: int
) -> Optional[MeliToken]:
    """
    Refresh an expired ML access token using the refresh token.

    Uses an asyncio lock per user to prevent race conditions when multiple
    concurrent requests try to refresh the same user's token simultaneously.
    """
    # Get or create a per-user lock
    if user_id not in _refresh_locks:
        _refresh_locks[user_id] = asyncio.Lock()
    lock = _refresh_locks[user_id]

    async with lock:
        try:
            result = await db.execute(
                select(MeliToken).where(MeliToken.user_id == user_id)
            )
            token_obj = result.scalar_one_or_none()

            if not token_obj:
                logger.warning(f"No ML token found for user {user_id}")
                return None

            # Check if another coroutine already refreshed while we waited
            if token_obj.expires_at and token_obj.expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                return token_obj

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    MELI_TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "client_id": settings.meli_client_id,
                        "client_secret": settings.meli_client_secret,
                        "refresh_token": token_obj.refresh_token,
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=15.0,
                )

            if response.status_code != 200:
                logger.error(f"ML token refresh failed: {response.status_code} - {response.text}")
                return None

            data = response.json()
            token_obj.access_token = data["access_token"]
            token_obj.refresh_token = data["refresh_token"]
            token_obj.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=data.get("expires_in", 21600)
            )

            await db.commit()
            await db.refresh(token_obj)
            logger.info(f"ML token refreshed for user {user_id}")
            return token_obj

        except Exception as e:
            logger.error(f"ML token refresh error: {e}")
            return None


async def get_valid_token(
    db: AsyncSession, user_id: int
) -> Optional[str]:
    """
    Get a valid ML access token for a user.
    Automatically refreshes if expired.
    """
    result = await db.execute(
        select(MeliToken).where(MeliToken.user_id == user_id)
    )
    token_obj = result.scalar_one_or_none()

    if not token_obj:
        return None

    # Check if token is expired (with 5 min buffer)
    if token_obj.expires_at and token_obj.expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
        logger.info(f"ML token expired for user {user_id}, refreshing...")
        token_obj = await refresh_meli_token(db, user_id)
        if not token_obj:
            return None

    return token_obj.access_token
