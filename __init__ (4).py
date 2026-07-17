"""SQLite implementation of the JournalRepository port."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from titan_ai_trader.application.interfaces.journal_repository import JournalRepository
from titan_ai_trader.domain.entities.journal import JournalEntry


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    entry = JournalEntry(
        content=row["content"],
        trade_id=row["trade_id"],
        tags=json.loads(row["tags"]),
        id=row["id"],
    )
    entry.created_at = datetime.fromisoformat(row["created_at"])
    entry.updated_at = datetime.fromisoformat(row["updated_at"])
    return entry


class SQLiteJournalRepository(JournalRepository):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def save(self, entry: JournalEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO journal_entries (
                id, content, trade_id, tags, created_at, updated_at
            ) VALUES (
                :id, :content, :trade_id, :tags, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                tags=excluded.tags,
                updated_at=excluded.updated_at
            """,
            {
                "id": entry.id,
                "content": entry.content,
                "trade_id": entry.trade_id,
                "tags": json.dumps(entry.tags),
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            },
        )
        self._conn.commit()

    def get_by_id(self, entry_id: str) -> JournalEntry | None:
        row = self._conn.execute(
            "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    def list_all(self) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM journal_entries ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def list_by_trade(self, trade_id: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM journal_entries WHERE trade_id = ? ORDER BY created_at DESC",
            (trade_id,),
        ).fetchall()
        return [_row_to_entry(row) for row in rows]
