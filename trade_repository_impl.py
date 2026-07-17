"""Application-wide constants.

TRADING_MODE is intentionally a hardcoded literal, not an environment
variable, for Phase 1. There is no code path in this codebase that can
place a live order -- no live broker adapter exists yet. This constant
exists so future phases have a single, obvious guard to check.
"""

from __future__ import annotations

from typing import Final, Literal

TradingMode = Literal["PAPER"]

TRADING_MODE: Final[TradingMode] = "PAPER"

DEFAULT_CURRENCY: Final[str] = "USD"
