"""Position entity.

A Position aggregates the net holding for a single symbol, derived from
one or more Trades. It tracks quantity, weighted-average entry price, and
running realized/unrealized P&L for that symbol within a Portfolio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from titan_ai_trader.domain.enums.trade_side import TradeSide
from titan_ai_trader.domain.exceptions.domain_exceptions import InvalidTradeError
from titan_ai_trader.domain.value_objects.money import Money


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Position:
    """Net open holding for one symbol."""

    symbol: str
    side: TradeSide
    quantity: Decimal
    average_entry_price: Money
    realized_pnl: Money = field(default_factory=lambda: Money.of(0))

    trade_ids: list[str] = field(default_factory=list)

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    opened_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidTradeError("Position quantity must be positive.")

    def is_flat(self) -> bool:
        return self.quantity == 0

    def add_fill(self, trade_id: str, fill_quantity: Decimal, fill_price: Money) -> None:
        """Merge an additional fill into this position (adds to size).

        Recomputes the weighted-average entry price.
        """
        if fill_quantity <= 0:
            raise InvalidTradeError("Fill quantity must be positive.")

        total_cost = (self.average_entry_price.amount * self.quantity) + (
            fill_price.amount * fill_quantity
        )
        new_quantity = self.quantity + fill_quantity
        self.average_entry_price = Money.of(
            total_cost / new_quantity, self.average_entry_price.currency
        )
        self.quantity = new_quantity
        self.trade_ids.append(trade_id)
        self._touch()

    def reduce(self, close_quantity: Decimal, exit_price: Money) -> Money:
        """Reduce position size (partial or full close) and return the
        realized P&L for the reduced portion."""
        if close_quantity <= 0 or close_quantity > self.quantity:
            raise InvalidTradeError("Invalid close quantity for this position.")

        diff = exit_price.amount - self.average_entry_price.amount
        if self.side == TradeSide.SHORT:
            diff = -diff
        pnl = Money.of(diff * close_quantity, exit_price.currency)

        self.realized_pnl = self.realized_pnl + pnl
        self.quantity = self.quantity - close_quantity
        self._touch()
        return pnl

    def unrealized_pnl(self, current_price: Money) -> Money:
        diff = current_price.amount - self.average_entry_price.amount
        if self.side == TradeSide.SHORT:
            diff = -diff
        return Money.of(diff * self.quantity, current_price.currency)

    def market_value(self, current_price: Money) -> Money:
        return Money.of(current_price.amount * self.quantity, current_price.currency)

    def _touch(self) -> None:
        self.updated_at = _utcnow()
