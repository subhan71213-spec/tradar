"""Abstract port for fetching the India VIX reading."""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.india_vix import IndiaVix


class IndiaVixProvider(ABC):
    @abstractmethod
    def get_vix(self) -> IndiaVix:
        """Fetch the latest India VIX reading.

        Raises MarketDataUnavailableError if the upstream source cannot be
        reached.
        """
