"""Abstract port for Portfolio persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.portfolio import Portfolio


class PortfolioRepository(ABC):
    @abstractmethod
    def save(self, portfolio: Portfolio) -> None:
        """Insert or update (upsert) a portfolio record."""

    @abstractmethod
    def get_by_name(self, name: str) -> Portfolio | None:
        """Fetch a portfolio by its unique name, or None if not found."""

    @abstractmethod
    def list_all(self) -> list[Portfolio]:
        """Return all portfolios."""
