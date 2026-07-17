"""SQLite implementation of the PositionRepository port."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from titan_ai_trader.application.interfaces.position_repository import PositionRepository
from titan_ai_trader.domain.entities.position import Position
from titan_ai_trader.domain.enums.trade_side import TradeSide
from titan_ai_trader.domain.value_objects.money import Money


def _row_to_position(row: sqlite3.Row) -> Position:
    currency = row["currency"]
    position = Position(
        symbol=row["symbol"],
        side=TradeSide(row["side"]),
        quantity=Decimal(row["quantity"]),
        average_entry_price=Money.of(row["average_entry_price"], currency),
        realized_pnl=Money.of(row["realized_pnl"], currency),
        trade_ids=json.loads(row["trade_ids"]),
        id=row["id"],
    )
    position.opened_at = datetime.fromisoformat(row["opened_at"])
    position.updated_at = datetime.fromisoformat(row["updated_at"])
    return position


class SQLitePositionRepository(PositionRepository):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def save(self, position: Position) -> None:
        self._conn.execute(
            """
            INSERT INTO positions (
                id, symbol, side, quantity, average_entry_price, currency,
                realized_pnl, trade_ids, opened_at, updated_at
            ) VALUES (
                :id, :symbol, :side, :quantity, :average_entry_price, :currency,
                :realized_pnl, :trade_ids, :opened_at, :updated_at
            )
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side,
                quantity=excluded.quantity,
                average_entry_price=excluded.average_entry_price,
                currency=excluded.currency,
                realized_pnl=excluded.realized_pnl,
                trade_ids=excluded.trade_ids,
                updated_at=excluded.updated_at
            """,
            {
                "id": position.id,
                "symbol": position.symbol,
                "side": position.side.value,
                "quantity": str(position.quantity),
                "average_entry_price": str(position.average_entry_price.amount),
                "currency": position.average_entry_price.currency,
                "realized_pnl": str(position.realized_pnl.amount),
                "trade_ids": json.dumps(position.trade_ids),
                "opened_at": position.opened_at.isoformat(),
                "updated_at": position.updated_at.isoformat(),
            },
        )
        self._conn.commit()

    def get_by_symbol(self, symbol: str) -> Position | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        return _row_to_position(row) if row else None

    def list_all(self) -> list[Position]:
        rows = self._conn.execute("SELECT * FROM positions").fetchall()
        return [_row_to_position(row) for row in rows]

    def delete(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self._conn.commit()
