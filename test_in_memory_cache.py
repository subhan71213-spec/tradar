"""OI Change summary value object — aggregated open-interest change."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StrikeOiChange:
    """OI change for one strike, split by call and put."""

    strike_price: Decimal
    call_oi_change: int
    put_oi_change: int


@dataclass(frozen=True, slots=True)
class OiChangeSummary:
    """Aggregated OI-change view across an entire option chain snapshot."""

    symbol: str
    total_call_oi_change: int
    total_put_oi_change: int
    by_strike: tuple[StrikeOiChange, ...]

    @property
    def net_oi_change(self) -> int:
        """Positive = net call OI build > put OI build (bearish-leaning writers)."""
        return self.total_call_oi_change - self.total_put_oi_change

    def top_call_writing_strikes(self, limit: int = 5) -> tuple[StrikeOiChange, ...]:
        return tuple(
            sorted(self.by_strike, key=lambda s: s.call_oi_change, reverse=True)[:limit]
        )

    def top_put_writing_strikes(self, limit: int = 5) -> tuple[StrikeOiChange, ...]:
        return tuple(
            sorted(self.by_strike, key=lambda s: s.put_oi_change, reverse=True)[:limit]
        )
