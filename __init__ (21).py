"""Max Pain value object — the result of a Max Pain calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)


@dataclass(frozen=True, slots=True)
class MaxPain:
    """The strike at which option writers collectively lose the least.

    `pain_by_strike` maps every candidate strike to the total notional
    loss option writers would face if the underlying expired there, so
    callers can inspect the full curve, not just the minimum.
    """

    symbol: str
    expiry_date: Date
    max_pain_strike: Decimal
    pain_by_strike: dict

    def __post_init__(self) -> None:
        if not self.pain_by_strike:
            raise MarketDataValidationError("MaxPain.pain_by_strike must not be empty.")
        if self.max_pain_strike not in self.pain_by_strike:
            raise MarketDataValidationError(
                "MaxPain.max_pain_strike must be a key in pain_by_strike."
            )
