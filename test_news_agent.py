"""Portfolio entity.

Tracks a single paper trading account: starting/current cash balance,
open positions, and realized P&L history. All money movement here is
simulated -- this entity has no connection to any real brokerage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from titan_ai_trader.domain.entities.position import Position
from titan_ai_trader.domain.exceptions.domain_exceptions import (
    InsufficientFundsError,
    PositionNotFoundError,
)
from titan_ai_trader.domain.value_objects.money import Money


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Portfolio:
    """A paper trading account: cash + open positions."""

    name: str
    starting_cash: Money
    cash: Money = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)  # keyed by symbol
    realized_pnl: Money = field(default_factory=lambda: Money.of(0))

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    def equity(self, current_prices: dict[str, Money]) -> Money:
        """Total account value: cash + market value of all open positions.

        `current_prices` maps symbol -> current Money price. Positions for
        symbols not present in the map are valued at their average entry
        price as a fallback.
        """
        total = self.cash
        for symbol, position in self.positions.items():
            price = current_prices.get(symbol, position.average_entry_price)
            total = total + position.market_value(price)
        return total

    def reserve_cash(self, amount: Money) -> None:
        if amount.amount > self.cash.amount:
            raise InsufficientFundsError(
                f"Insufficient paper cash: have {self.cash}, need {amount}."
            )
        self.cash = self.cash - amount
        self._touch()

    def release_cash(self, amount: Money) -> None:
        self.cash = self.cash + amount
        self._touch()

    def get_position(self, symbol: str) -> Position:
        position = self.positions.get(symbol)
        if position is None:
            raise PositionNotFoundError(f"No open position for symbol {symbol}.")
        return position

    def upsert_position(self, position: Position) -> None:
        self.positions[position.symbol] = position
        self._touch()

    def close_position(self, symbol: str) -> Position:
        position = self.get_position(symbol)
        del self.positions[symbol]
        self.realized_pnl = self.realized_pnl + position.realized_pnl
        self._touch()
        return position

    def record_realized_pnl(self, pnl: Money) -> None:
        self.realized_pnl = self.realized_pnl + pnl
        self._touch()

    def _touch(self) -> None:
        self.updated_at = _utcnow()
