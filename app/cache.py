"""In-memory cache with TTL support.

Replaces Redis for meli-api's caching needs (categories, attribute values, PKCE state).
Thread-safe via asyncio (single-threaded event loop).
"""

import time
from typing import Any, Optional


class TTLCache:
    """Simple in-memory key-value store with per-key TTL."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)

    async def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at and time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: Any, ex: int = 0) -> None:
        expires_at = (time.monotonic() + ex) if ex > 0 else 0.0
        self._store[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def ttl(self, key: str) -> int:
        entry = self._store.get(key)
        if entry is None:
            return -2
        _, expires_at = entry
        if not expires_at:
            return -1
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            del self._store[key]
            return -2
        return int(remaining)

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None


# Global cache instance
cache = TTLCache()
