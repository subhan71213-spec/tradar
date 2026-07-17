from __future__ import annotations

import time

from titan_ai_trader.infrastructure.market_data.cache.in_memory_cache import (
    InMemoryMarketDataCache,
)


def test_set_then_get_returns_value():
    cache = InMemoryMarketDataCache()
    cache.set("key1", {"a": 1}, ttl_seconds=60)
    assert cache.get("key1") == {"a": 1}


def test_get_missing_key_returns_none():
    cache = InMemoryMarketDataCache()
    assert cache.get("nope") is None


def test_expired_entry_returns_none():
    cache = InMemoryMarketDataCache()
    cache.set("key1", "value", ttl_seconds=0.01)
    time.sleep(0.05)
    assert cache.get("key1") is None


def test_invalidate_removes_key():
    cache = InMemoryMarketDataCache()
    cache.set("key1", "value", ttl_seconds=60)
    cache.invalidate("key1")
    assert cache.get("key1") is None


def test_invalidate_missing_key_is_a_no_op():
    cache = InMemoryMarketDataCache()
    cache.invalidate("does-not-exist")  # must not raise


def test_clear_removes_everything():
    cache = InMemoryMarketDataCache()
    cache.set("key1", "a", ttl_seconds=60)
    cache.set("key2", "b", ttl_seconds=60)
    cache.clear()
    assert cache.get("key1") is None
    assert cache.get("key2") is None


def test_set_overwrites_existing_key():
    cache = InMemoryMarketDataCache()
    cache.set("key1", "first", ttl_seconds=60)
    cache.set("key1", "second", ttl_seconds=60)
    assert cache.get("key1") == "second"
