"""Put-Call Ratio value object — the result of a PCR calculation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)


@dataclass(frozen=True, slots=True)
class Pcr:
    """Immutable result of a Put-Call Ratio computation.

    Conventionally: PCR > 1 is read as bearish-leaning positioning
    (more put OI than call OI), PCR < 1 as bullish-leaning. This object
    only carries the numbers; interpretation stays with the caller.
    """

    symbol: str
    total_put_open_interest: int
    total_call_open_interest: int
    ratio: Decimal

    def __post_init__(self) -> None:
        if self.total_put_open_interest < 0 or self.total_call_open_interest < 0:
            raise MarketDataValidationError("Pcr open interest totals cannot be negative.")
        if self.ratio < 0:
            raise MarketDataValidationError("Pcr.ratio cannot be negative.")
