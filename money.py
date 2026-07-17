"""Abstract port for caching market data.

Defined in the application layer so MarketDataService can depend on this
interface without knowing whether an in-memory dict, Redis, or something
else is behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MarketDataCache(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None:
        """Return the cached value for key, or None if missing/expired."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Cache value under key for ttl_seconds."""

    @abstractmethod
    def invalidate(self, key: str) -> None:
        """Remove a key from the cache, if present."""

    @abstractmethod
    def clear(self) -> None:
        """Remove everything from the cache."""
