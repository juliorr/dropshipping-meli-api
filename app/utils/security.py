"""Security utilities - JWT decode only (no password hashing needed in meli-api)."""

from typing import Any, Optional

from jose import JWTError, jwt

from app.config import settings


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate a JWT token. Returns payload or None if invalid."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        return None
