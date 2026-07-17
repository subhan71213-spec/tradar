"""Abstract port for fetching NSE spot (equity/index) quotes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.market_quote import MarketQuote


class NseSpotProvider(ABC):
    @abstractmethod
    def get_spot(self, symbol: str, is_index: bool = False) -> MarketQuote:
        """Fetch the latest spot quote for symbol.

        Set is_index=True for index symbols (e.g. 'NIFTY 50', 'NIFTY BANK'),
        False for equity symbols (e.g. 'RELIANCE', 'TCS') -- NSE serves
        these from different endpoints.

        Raises MarketDataUnavailableError if the upstream source cannot be
        reached, and SymbolNotFoundError if the symbol does not exist.
        """
