"""Application settings.

Deliberately dependency-free (plain dataclass) for Phase 1 so the
persistence layer has no third-party requirements beyond the standard
library. Values are read from environment variables with safe defaults,
mirroring what a pydantic-settings class would do, without requiring it
to be installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from titan_ai_trader.shared.constants import TRADING_MODE, TradingMode


def _database_path_default() -> Path:
    return Path(os.environ.get("TITAN_DB_PATH", "titan_paper_trading.db"))


def _starting_cash_default() -> str:
    return os.environ.get("TITAN_STARTING_CASH", "100000")


def _market_data_timeout_default() -> float:
    return float(os.environ.get("TITAN_MARKET_DATA_TIMEOUT", "10.0"))


def _market_data_max_retries_default() -> int:
    return int(os.environ.get("TITAN_MARKET_DATA_MAX_RETRIES", "3"))


@dataclass(frozen=True, slots=True)
class Settings:
    trading_mode: TradingMode = TRADING_MODE
    # NOTE: these use default_factory (evaluated fresh on every Settings()
    # call), not a plain `= os.environ.get(...)` default (which Python
    # would evaluate exactly once, at class-definition/import time, and
    # then silently ignore any later change to the environment -- e.g. a
    # .env file loaded by main.py after this module was first imported).
    database_path: Path = field(default_factory=_database_path_default)
    default_starting_cash: str = field(default_factory=_starting_cash_default)

    # Market data layer (Phase 2)
    market_data_http_timeout_seconds: float = field(
        default_factory=_market_data_timeout_default
    )
    market_data_max_retry_attempts: int = field(
        default_factory=_market_data_max_retries_default
    )

    def __post_init__(self) -> None:
        if self.trading_mode != "PAPER":
            # Guard rail: Phase 1 supports paper trading only. This branch
            # should be unreachable given TRADING_MODE is a Literal["PAPER"],
            # but it is kept as an explicit fail-safe.
            raise RuntimeError(
                "Only PAPER trading mode is supported in this build. "
                "No live broker adapter is implemented."
            )


def get_settings() -> Settings:
    return Settings()
