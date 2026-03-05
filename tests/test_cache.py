"""Tests for app/cache.py TTLCache."""

import time

import pytest

from app.cache import TTLCache


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache()


# ---------------------------------------------------------------------------
# Basic set / get
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get(cache):
    await cache.set("key1", "value1")
    assert await cache.get("key1") == "value1"


@pytest.mark.asyncio
async def test_get_nonexistent_key_returns_none(cache):
    assert await cache.get("does_not_exist") is None


@pytest.mark.asyncio
async def test_set_and_get_various_types(cache):
    await cache.set("int_key", 42)
    await cache.set("dict_key", {"a": 1, "b": [1, 2, 3]})
    await cache.set("list_key", [1, 2, 3])

    assert await cache.get("int_key") == 42
    assert await cache.get("dict_key") == {"a": 1, "b": [1, 2, 3]}
    assert await cache.get("list_key") == [1, 2, 3]


@pytest.mark.asyncio
async def test_overwrite_key(cache):
    await cache.set("key", "first")
    await cache.set("key", "second")
    assert await cache.get("key") == "second"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_expiry(cache):
    await cache.set("expiring", "bye", ex=1)
    assert await cache.get("expiring") == "bye"
    time.sleep(1.1)
    assert await cache.get("expiring") is None


@pytest.mark.asyncio
async def test_no_ttl_never_expires(cache):
    await cache.set("permanent", "stays", ex=0)
    time.sleep(0.05)
    assert await cache.get("permanent") == "stays"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_existing_key(cache):
    await cache.set("to_delete", "value")
    await cache.delete("to_delete")
    assert await cache.get("to_delete") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_key_no_error(cache):
    await cache.delete("ghost_key")  # should not raise


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exists_true_for_existing(cache):
    await cache.set("present", 123)
    assert await cache.exists("present") is True


@pytest.mark.asyncio
async def test_exists_false_for_missing(cache):
    assert await cache.exists("absent") is False


@pytest.mark.asyncio
async def test_exists_false_after_expiry(cache):
    await cache.set("short_lived", "x", ex=1)
    time.sleep(1.1)
    assert await cache.exists("short_lived") is False


# ---------------------------------------------------------------------------
# ttl
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_returns_remaining_seconds(cache):
    await cache.set("timed", "val", ex=10)
    remaining = await cache.ttl("timed")
    assert 8 <= remaining <= 10


@pytest.mark.asyncio
async def test_ttl_minus_one_for_no_expiry(cache):
    await cache.set("permanent", "val", ex=0)
    assert await cache.ttl("permanent") == -1


@pytest.mark.asyncio
async def test_ttl_minus_two_for_missing_key(cache):
    assert await cache.ttl("no_key") == -2


@pytest.mark.asyncio
async def test_ttl_minus_two_after_expiry(cache):
    await cache.set("gone", "x", ex=1)
    time.sleep(1.1)
    assert await cache.ttl("gone") == -2


# ---------------------------------------------------------------------------
# Multiple independent keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_independent_keys(cache):
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.set("c", 3)

    assert await cache.get("a") == 1
    assert await cache.get("b") == 2
    assert await cache.get("c") == 3

    await cache.delete("b")
    assert await cache.get("a") == 1
    assert await cache.get("b") is None
    assert await cache.get("c") == 3
