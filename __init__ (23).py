"""IndiaVix entity — the NSE volatility index reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from titan_ai_trader.domain.exceptions.market_data_exceptions import (
    MarketDataValidationError,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IndiaVix:
    """A single India VIX observation."""

    value: Decimal
    change: Decimal
    change_percent: Decimal
    timestamp: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        # India VIX has never traded outside roughly this band historically;
        # treat wildly out-of-range values as bad data rather than silently
        # accepting them.
        if self.value <= 0 or self.value > 200:
            raise MarketDataValidationError(
                f"IndiaVix.value {self.value} is outside a plausible range (0, 200]."
            )

    @property
    def is_elevated(self) -> bool:
        """Conventional rule-of-thumb threshold for elevated volatility."""
        return self.value >= Decimal("20")
