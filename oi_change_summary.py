"""Abstract port for fetching FII/DII cash market activity."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date as Date

from titan_ai_trader.domain.entities.fii_dii_activity import FiiDiiActivity


class FiiDiiProvider(ABC):
    @abstractmethod
    def get_latest(self) -> FiiDiiActivity:
        """Fetch the most recently published FII/DII activity figures.

        Raises MarketDataUnavailableError if the upstream source cannot be
        reached.
        """

    @abstractmethod
    def get_by_date(self, activity_date: Date) -> FiiDiiActivity:
        """Fetch FII/DII activity for a specific past trading date.

        Raises MarketDataUnavailableError if unreachable, SymbolNotFoundError
        (repurposed here as "no data for that date") if no figures were
        published for that date.
        """
