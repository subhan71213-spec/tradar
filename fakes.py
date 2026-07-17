"""Trade entity.

The central object of the paper trading engine. A Trade captures a single
position lifecycle: entry, protective stop, up to three profit targets,
an optional trailing stop, its current status, realized/unrealized P&L,
and free-form notes (the trading journal narrative for this trade).

This module is pure domain code: no SQLAlchemy, no I/O, no framework
dependencies. It is safe to unit test in isolation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from titan_ai_trader.domain.enums.trade_side import TradeSide
from titan_ai_trader.domain.enums.trade_status import TradeStatus
from titan_ai_trader.domain.exceptions.domain_exceptions import (
    InvalidTargetLevelsError,
    InvalidTradeError,
    TradeAlreadyClosedError,
)
from titan_ai_trader.domain.value_objects.money import Money


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Trade:
    """A single paper trade (one symbol, one direction, one entry)."""

    symbol: str
    side: TradeSide
    entry_price: Money
    quantity: Decimal

    stop_loss: Money | None = None
    target_1: Money | None = None
    target_2: Money | None = None
    target_3: Money | None = None

    # Trailing stop is expressed as a distance from the best favorable price
    # seen since entry. trailing_stop_price is the live, computed stop level.
    trailing_stop_distance: Money | None = None
    trailing_stop_price: Money | None = None
    _best_price_seen: Money | None = field(default=None, repr=False)

    status: TradeStatus = TradeStatus.PENDING
    exit_price: Money | None = None
    realized_pnl: Money | None = None
    target_hit: int | None = None  # which target (1, 2, or 3) closed the trade, if any

    notes: str = ""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    opened_at: datetime = field(default_factory=_utcnow)
    closed_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidTradeError("Trade quantity must be positive.")
        if not self.symbol:
            raise InvalidTradeError("Trade symbol must not be empty.")
        self._validate_levels()

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def _validate_levels(self) -> None:
        """Ensure stop loss / targets are on the correct side of entry."""
        entry = self.entry_price.amount
        is_long = self.side == TradeSide.LONG

        if self.stop_loss is not None:
            sl = self.stop_loss.amount
            if is_long and sl >= entry:
                raise InvalidTargetLevelsError(
                    "Stop loss for a LONG trade must be below entry price."
                )
            if not is_long and sl <= entry:
                raise InvalidTargetLevelsError(
                    "Stop loss for a SHORT trade must be above entry price."
                )

        targets = [self.target_1, self.target_2, self.target_3]
        prev = entry
        for i, target in enumerate(targets, start=1):
            if target is None:
                continue
            t = target.amount
            if is_long and t <= prev:
                raise InvalidTargetLevelsError(
                    f"Target {i} must be greater than entry/previous target for a LONG trade."
                )
            if not is_long and t >= prev:
                raise InvalidTargetLevelsError(
                    f"Target {i} must be less than entry/previous target for a SHORT trade."
                )
            prev = t

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        if self.status != TradeStatus.PENDING:
            raise InvalidTradeError(f"Cannot open a trade in status {self.status}.")
        self.status = TradeStatus.OPEN
        self.opened_at = _utcnow()
        self._best_price_seen = self.entry_price
        self._touch()

    def _ensure_open(self) -> None:
        if self.status != TradeStatus.OPEN:
            raise TradeAlreadyClosedError(
                f"Trade {self.id} is not open (status={self.status})."
            )

    def close(self, exit_price: Money, status: TradeStatus, reason: str = "") -> None:
        """Close the trade at a given exit price with a terminal status."""
        self._ensure_open()
        if not status.is_terminal:
            raise InvalidTradeError(f"{status} is not a terminal status.")

        self.exit_price = exit_price
        self.status = status
        self.closed_at = _utcnow()
        self.realized_pnl = self.calculate_pnl(exit_price)
        if reason:
            self.add_note(reason)
        self._touch()

    def cancel(self, reason: str = "") -> None:
        if self.status != TradeStatus.PENDING:
            raise InvalidTradeError("Only a PENDING trade can be cancelled.")
        self.status = TradeStatus.CANCELLED
        self.closed_at = _utcnow()
        if reason:
            self.add_note(reason)
        self._touch()

    # ------------------------------------------------------------------ #
    # P&L
    # ------------------------------------------------------------------ #
    def calculate_pnl(self, current_price: Money) -> Money:
        """Signed P&L (positive = profit) at the given market price."""
        diff = current_price.amount - self.entry_price.amount
        if self.side == TradeSide.SHORT:
            diff = -diff
        return Money.of(diff * self.quantity, self.entry_price.currency)

    def unrealized_pnl(self, current_price: Money) -> Money:
        self._ensure_open()
        return self.calculate_pnl(current_price)

    # ------------------------------------------------------------------ #
    # Stop / target / trailing-stop evaluation
    # ------------------------------------------------------------------ #
    def update_trailing_stop(self, current_price: Money) -> None:
        """Advance the trailing stop as price moves favorably.

        No-op if the trade has no trailing_stop_distance configured, or if
        price has not made a new favorable extreme.
        """
        self._ensure_open()
        if self.trailing_stop_distance is None:
            return

        if self._best_price_seen is None:
            self._best_price_seen = self.entry_price

        is_long = self.side == TradeSide.LONG
        improved = (
            current_price.amount > self._best_price_seen.amount
            if is_long
            else current_price.amount < self._best_price_seen.amount
        )
        if not improved:
            return

        self._best_price_seen = current_price
        distance = self.trailing_stop_distance.amount
        new_stop = (
            current_price.amount - distance if is_long else current_price.amount + distance
        )

        if self.trailing_stop_price is None:
            self.trailing_stop_price = Money.of(new_stop, current_price.currency)
        else:
            better = (
                new_stop > self.trailing_stop_price.amount
                if is_long
                else new_stop < self.trailing_stop_price.amount
            )
            if better:
                self.trailing_stop_price = Money.of(new_stop, current_price.currency)
        self._touch()

    def evaluate(self, current_price: Money) -> TradeStatus | None:
        """Check current price against stop/trailing-stop/targets.

        Returns the terminal status the trade WOULD close with, or None if
        the trade should remain open. Does not mutate trade state (call
        `close()` separately to commit the transition) except for advancing
        the trailing stop, which is a live, non-terminal state update.
        """
        self._ensure_open()
        self.update_trailing_stop(current_price)
        is_long = self.side == TradeSide.LONG
        price = current_price.amount

        active_stop = self.trailing_stop_price or self.stop_loss
        if active_stop is not None:
            hit = price <= active_stop.amount if is_long else price >= active_stop.amount
            if hit:
                return TradeStatus.STOPPED_OUT

        for level in (self.target_3, self.target_2, self.target_1):
            if level is None:
                continue
            hit = price >= level.amount if is_long else price <= level.amount
            if hit:
                return TradeStatus.TARGET_HIT

        return None

    # ------------------------------------------------------------------ #
    # Journal notes
    # ------------------------------------------------------------------ #
    def add_note(self, text: str) -> None:
        if not text:
            return
        timestamp = _utcnow().isoformat(timespec="seconds")
        entry = f"[{timestamp}] {text}"
        self.notes = f"{self.notes}\n{entry}".strip()
        self._touch()

    def _touch(self) -> None:
        self.updated_at = _utcnow()
