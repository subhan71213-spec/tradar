"""Abstract port for Trade persistence.

Defined in the application layer so use cases can depend on this
interface without knowing SQLite (or any other storage) is behind it.
Implemented by infrastructure/persistence/trade_repository_impl.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.trade import Trade
from titan_ai_trader.domain.enums.trade_status import TradeStatus


class TradeRepository(ABC):
    @abstractmethod
    def save(self, trade: Trade) -> None:
        """Insert or update (upsert) a trade record."""

    @abstractmethod
    def get_by_id(self, trade_id: str) -> Trade | None:
        """Fetch a single trade by id, or None if not found."""

    @abstractmethod
    def list_all(self) -> list[Trade]:
        """Return every trade ever recorded, most recent first."""

    @abstractmethod
    def list_by_status(self, status: TradeStatus) -> list[Trade]:
        """Return all trades currently in the given status."""

    @abstractmethod
    def list_by_symbol(self, symbol: str) -> list[Trade]:
        """Return all trades (any status) for a given symbol."""
