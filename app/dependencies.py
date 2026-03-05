"""FastAPI dependencies for meli-api."""

from typing import AsyncGenerator, NamedTuple, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.utils.security import decode_token

security = HTTPBearer(auto_error=False)


class AuthUser(NamedTuple):
    """Lightweight user representation extracted from JWT — no DB lookup needed."""
    id: int
    is_superuser: bool


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to get database session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthUser:
    """
    Validate JWT and return user identity.
    Accepts token from httpOnly cookie or Authorization header.
    The meli-api shares the same JWT_SECRET as the backend so tokens issued
    by the backend are accepted here without a separate login.
    No DB lookup — user_id and is_superuser are embedded in the token payload.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Try httpOnly cookie first, then Authorization header
    token = request.cookies.get("access_token")
    if not token and credentials:
        token = credentials.credentials
    if not token:
        raise credentials_exception

    payload = decode_token(token)
    if payload is None or payload.get("type") != "access":
        raise credentials_exception

    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    return AuthUser(
        id=int(user_id),
        is_superuser=payload.get("is_superuser", False),
    )


async def verify_api_key(x_api_key: str = Header(...)) -> None:
    """
    Verify service-to-service API Key used by the backend when calling meli-api.
    """
    if x_api_key != settings.meli_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
