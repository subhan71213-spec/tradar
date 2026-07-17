"""Abstract port for Position persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.position import Position


class PositionRepository(ABC):
    @abstractmethod
    def save(self, position: Position) -> None:
        """Insert or update (upsert) a position record, keyed by symbol."""

    @abstractmethod
    def get_by_symbol(self, symbol: str) -> Position | None:
        """Fetch the open position for a symbol, or None if flat."""

    @abstractmethod
    def list_all(self) -> list[Position]:
        """Return all currently tracked positions."""

    @abstractmethod
    def delete(self, symbol: str) -> None:
        """Remove a position record (e.g. once fully closed)."""
