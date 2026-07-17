"""Abstract port for Journal persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod

from titan_ai_trader.domain.entities.journal import JournalEntry


class JournalRepository(ABC):
    @abstractmethod
    def save(self, entry: JournalEntry) -> None:
        """Insert or update (upsert) a journal entry."""

    @abstractmethod
    def get_by_id(self, entry_id: str) -> JournalEntry | None:
        """Fetch a single journal entry by id, or None if not found."""

    @abstractmethod
    def list_all(self) -> list[JournalEntry]:
        """Return every journal entry, most recent first."""

    @abstractmethod
    def list_by_trade(self, trade_id: str) -> list[JournalEntry]:
        """Return all journal entries linked to a specific trade."""
