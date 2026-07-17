"""Enumerations describing the lifecycle state of a paper trade."""

from __future__ import annotations

from enum import StrEnum


class TradeStatus(StrEnum):
    """Lifecycle states a Trade can occupy.

    A trade is always PENDING -> OPEN -> one terminal state.
    Terminal states are CLOSED, STOPPED_OUT, TARGET_HIT, and CANCELLED.
    """

    PENDING = "PENDING"            # created, not yet filled
    OPEN = "OPEN"                  # filled, position live
    CLOSED = "CLOSED"              # manually/fully closed
    STOPPED_OUT = "STOPPED_OUT"    # closed via stop loss / trailing stop
    TARGET_HIT = "TARGET_HIT"      # closed via a target level
    CANCELLED = "CANCELLED"        # cancelled before fill

    @property
    def is_terminal(self) -> bool:
        return self in {
            TradeStatus.CLOSED,
            TradeStatus.STOPPED_OUT,
            TradeStatus.TARGET_HIT,
            TradeStatus.CANCELLED,
        }
