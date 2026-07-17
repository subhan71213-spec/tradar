"""Identifies which upstream feed a piece of market data came from.

Kept as an enum (rather than a free string) so callers and log lines are
consistent, and so a future data source can be added in one place.
"""

from __future__ import annotations

from enum import StrEnum


class MarketDataSource(StrEnum):
    NSE_SPOT = "NSE_SPOT"
    NSE_OPTION_CHAIN = "NSE_OPTION_CHAIN"
    INDIA_VIX = "INDIA_VIX"
    FII_DII = "FII_DII"
