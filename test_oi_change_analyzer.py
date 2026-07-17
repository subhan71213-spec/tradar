"""MarketQuote entity — a spot price snapshot for one instrument.

Used for NSE equity/index spot data (e.g. NIFTY 50, BANKNIFTY, RELIANCE).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)
from titan_ai_trader.domain.value_objects.money import Money


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """A single spot price observation for one symbol."""

    symbol: str
    last_price: Money
    change: Decimal
    change_percent: Decimal
    volume: int | None = None
    open_price: Money | None = None
    high_price: Money | None = None
    low_price: Money | None = None
    previous_close: Money | None = None
    timestamp: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise MarketDataValidationError("MarketQuote.symbol must not be empty.")
        if self.last_price.amount <= 0:
            raise MarketDataValidationError(
                f"MarketQuote.last_price must be positive, got {self.last_price}."
            )
        if self.volume is not None and self.volume < 0:
            raise MarketDataValidationError("MarketQuote.volume cannot be negative.")
