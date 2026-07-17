"""FiiDiiActivity entity — daily FII/DII cash market participation.

Figures are published end-of-day by NSE/exchanges in crore INR. Net
values are derived (buy - sell), never taken as given, so they can never
silently drift from the underlying buy/sell figures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)
from titan_ai_trader.domain.value_objects.money import Money


@dataclass(frozen=True, slots=True)
class FiiDiiActivity:
    """FII and DII gross buy/sell figures for a single trading day."""

    activity_date: Date
    fii_buy_value: Money
    fii_sell_value: Money
    dii_buy_value: Money
    dii_sell_value: Money

    def __post_init__(self) -> None:
        for label, money in (
            ("fii_buy_value", self.fii_buy_value),
            ("fii_sell_value", self.fii_sell_value),
            ("dii_buy_value", self.dii_buy_value),
            ("dii_sell_value", self.dii_sell_value),
        ):
            if money.amount < 0:
                raise MarketDataValidationError(f"FiiDiiActivity.{label} cannot be negative.")

    @property
    def fii_net_value(self) -> Money:
        return self.fii_buy_value - self.fii_sell_value

    @property
    def dii_net_value(self) -> Money:
        return self.dii_buy_value - self.dii_sell_value

    @property
    def is_fii_net_buyer(self) -> bool:
        return not self.fii_net_value.is_negative()

    @property
    def is_dii_net_buyer(self) -> bool:
        return not self.dii_net_value.is_negative()
