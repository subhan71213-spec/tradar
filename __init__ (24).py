"""OptionContract entity — one strike/type row from an NSE option chain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal

from titan_ai_trader.domain.enums.option_type import OptionType
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)
from titan_ai_trader.domain.value_objects.money import Money


@dataclass(frozen=True, slots=True)
class OptionContract:
    """A single CE or PE row for one strike and expiry."""

    strike_price: Decimal
    option_type: OptionType
    expiry_date: Date
    open_interest: int
    change_in_open_interest: int
    volume: int
    implied_volatility: Decimal | None
    last_price: Money
    bid_price: Money | None = None
    ask_price: Money | None = None

    def __post_init__(self) -> None:
        if self.strike_price <= 0:
            raise MarketDataValidationError("OptionContract.strike_price must be positive.")
        if self.open_interest < 0:
            raise MarketDataValidationError("OptionContract.open_interest cannot be negative.")
        if self.volume < 0:
            raise MarketDataValidationError("OptionContract.volume cannot be negative.")
        if self.last_price.amount < 0:
            raise MarketDataValidationError("OptionContract.last_price cannot be negative.")
        if self.implied_volatility is not None and self.implied_volatility < 0:
            raise MarketDataValidationError(
                "OptionContract.implied_volatility cannot be negative."
            )
