"""Abstract port for fetching NSE option chain data."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date as Date

from titan_ai_trader.domain.entities.option_chain_snapshot import OptionChainSnapshot


class OptionChainProvider(ABC):
    @abstractmethod
    def get_option_chain(
        self, symbol: str, expiry_date: Date | None = None
    ) -> OptionChainSnapshot:
        """Fetch the option chain for symbol.

        If expiry_date is None, the nearest available expiry is used.
        Raises MarketDataUnavailableError if the upstream source cannot be
        reached, and SymbolNotFoundError if the symbol does not exist.
        """
