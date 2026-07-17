"""In-memory TTL cache for market data.

Process-local and not persisted -- appropriate for a single paper trading
process. Swappable for a Redis-backed implementation later without
touching MarketDataService, since both implement MarketDataCache.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from titan_ai_trader.application.interfaces.market_data_cache import MarketDataCache


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float


class InMemoryMarketDataCache(MarketDataCache):
    """Thread-safe in-memory cache with per-key TTL."""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at < time.monotonic():
                del self._store[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._store[key] = _CacheEntry(
                value=value, expires_at=time.monotonic() + ttl_seconds
            )

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
