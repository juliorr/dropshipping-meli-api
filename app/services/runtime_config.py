"""Runtime config service — in-memory cache backed by DB.

Allows API keys and feature flags to be changed from the admin UI
without restarting the service. Falls back to env vars → defaults.
"""

import logging
import os
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)

# In-memory cache: key → value
_cache: Dict[str, str] = {}

# Predefined config keys with defaults and metadata
CONFIG_KEYS: Dict[str, Dict[str, Any]] = {
    "anthropic_api_key": {
        "default": "",
        "is_secret": True,
        "description": "Claude API key for LLM fix discovery",
        "env_var": "ANTHROPIC_API_KEY",
    },
    "llm_fix_enabled": {
        "default": "false",
        "is_secret": False,
        "description": "Enable LLM auto-fix for unknown errors (true/false)",
        "env_var": "LLM_FIX_ENABLED",
    },
    "github_token": {
        "default": "",
        "is_secret": True,
        "description": "GitHub personal access token for PR creation",
        "env_var": "GITHUB_TOKEN",
    },
    "anthropic_model": {
        "default": "claude-sonnet-4-20250514",
        "is_secret": False,
        "description": "Claude model ID for LLM calls",
        "env_var": "ANTHROPIC_MODEL",
    },
}


def get_config(key: str, default: Optional[str] = None) -> str:
    """Get a config value: cache → env var → default.

    This is a synchronous function safe to call from anywhere.
    """
    # 1. Check in-memory cache (populated from DB)
    if key in _cache and _cache[key]:
        return _cache[key]

    # 2. Check env var
    meta = CONFIG_KEYS.get(key, {})
    env_var = meta.get("env_var", key.upper())
    env_value = os.environ.get(env_var, "")
    if env_value:
        return env_value

    # 3. Fall back to default
    if default is not None:
        return default
    return meta.get("default", "")


def get_config_bool(key: str) -> bool:
    """Get a config value as boolean."""
    return get_config(key).lower() in ("true", "1", "yes")


def mask_secret(value: str) -> str:
    """Mask a secret value for display: 'sk-ant-abc123xyz' → 'sk-ant-***xyz'."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:6] + "***" + value[-3:]


def get_all_config_masked() -> list[Dict[str, Any]]:
    """Get all config keys with masked secret values for API response."""
    result = []
    for key, meta in CONFIG_KEYS.items():
        value = get_config(key)
        display_value = mask_secret(value) if meta["is_secret"] else value
        result.append({
            "key": key,
            "value": display_value,
            "is_secret": meta["is_secret"],
            "description": meta["description"],
            "has_value": bool(value),
        })
    return result


async def set_config(
    db: AsyncSession,
    key: str,
    value: str,
    updated_by: Optional[int] = None,
) -> None:
    """Write a config value to DB and update the in-memory cache."""
    meta = CONFIG_KEYS.get(key)
    if not meta:
        raise ValueError(f"Unknown config key: {key}")

    existing = (
        await db.execute(
            select(RuntimeConfig).where(RuntimeConfig.key == key)
        )
    ).scalar_one_or_none()

    if existing:
        existing.value = value
        existing.updated_by = updated_by
    else:
        row = RuntimeConfig(
            key=key,
            value=value,
            is_secret=meta["is_secret"],
            description=meta["description"],
            updated_by=updated_by,
        )
        db.add(row)

    await db.commit()

    # Update in-memory cache
    _cache[key] = value
    logger.info(
        f"[CONFIG] Updated '{key}' "
        f"({'***' if meta['is_secret'] else value}) "
        f"by user {updated_by}"
    )


async def load_config_cache() -> None:
    """Load all runtime config from DB into memory. Call on startup."""
    from app.database import async_session

    async with async_session() as db:
        result = await db.execute(select(RuntimeConfig))
        rows = result.scalars().all()

        loaded = 0
        for row in rows:
            _cache[row.key] = row.value
            loaded += 1

    logger.info(f"[CONFIG] Loaded {loaded} runtime config value(s) from DB")
