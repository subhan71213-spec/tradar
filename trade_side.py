"""Domain-level exceptions for the market data layer. Framework-free."""

from __future__ import annotations


class MarketDataError(Exception):
    """Base class for all market data errors."""


class MarketDataValidationError(MarketDataError):
    """Raised when incoming market data fails a sanity/consistency check."""


class StaleMarketDataError(MarketDataError):
    """Raised when a market data snapshot is older than an allowed threshold."""


class MarketDataUnavailableError(MarketDataError):
    """Raised when an upstream source could not be reached after retries."""


class SymbolNotFoundError(MarketDataError):
    """Raised when a requested symbol/instrument does not exist upstream."""
