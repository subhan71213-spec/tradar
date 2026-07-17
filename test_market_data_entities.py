"""OptionChainSnapshot entity — the full option chain for a symbol at a
point in time. This is the raw material that PCR, Max Pain, and OI Change
are computed from (see domain/services/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as Date

from titan_ai_trader.domain.entities.option_contract import OptionContract
from titan_ai_trader.domain.enums.option_type import OptionType
from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)
from titan_ai_trader.domain.value_objects.money import Money


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """All strikes/types for one symbol and one expiry, at one timestamp."""

    symbol: str
    expiry_date: Date
    underlying_value: Money
    contracts: tuple[OptionContract, ...]
    timestamp: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise MarketDataValidationError("OptionChainSnapshot.symbol must not be empty.")
        if not self.contracts:
            raise MarketDataValidationError(
                "OptionChainSnapshot must contain at least one contract."
            )
        mismatched = [c for c in self.contracts if c.expiry_date != self.expiry_date]
        if mismatched:
            raise MarketDataValidationError(
                "All OptionContracts in a snapshot must share the snapshot's expiry_date."
            )

    @property
    def calls(self) -> tuple[OptionContract, ...]:
        return tuple(c for c in self.contracts if c.option_type == OptionType.CE)

    @property
    def puts(self) -> tuple[OptionContract, ...]:
        return tuple(c for c in self.contracts if c.option_type == OptionType.PE)

    @property
    def strikes(self) -> tuple:
        return tuple(sorted({c.strike_price for c in self.contracts}))
