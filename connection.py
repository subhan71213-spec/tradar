"""SQLite implementation of the TradeRepository port.

Every trade placed through the paper trading engine is persisted here.
No live broker is involved anywhere in this module -- it only reads and
writes rows in a local SQLite database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from titan_ai_trader.application.interfaces.trade_repository import TradeRepository
from titan_ai_trader.domain.entities.trade import Trade
from titan_ai_trader.domain.enums.trade_side import TradeSide
from titan_ai_trader.domain.enums.trade_status import TradeStatus
from titan_ai_trader.domain.value_objects.money import Money


def _money_or_none(text: str | None, currency: str) -> Money | None:
    return Money.of(text, currency) if text is not None else None


def _text_or_none(money: Money | None) -> str | None:
    return str(money.amount) if money is not None else None


def _row_to_trade(row: sqlite3.Row) -> Trade:
    currency = row["currency"]
    trade = Trade(
        symbol=row["symbol"],
        side=TradeSide(row["side"]),
        entry_price=Money.of(row["entry_price"], currency),
        quantity=Decimal(row["quantity"]),
        stop_loss=_money_or_none(row["stop_loss"], currency),
        target_1=_money_or_none(row["target_1"], currency),
        target_2=_money_or_none(row["target_2"], currency),
        target_3=_money_or_none(row["target_3"], currency),
        trailing_stop_distance=_money_or_none(row["trailing_stop_distance"], currency),
        notes="",  # set below, bypassing add_note's timestamp-prefixing
        id=row["id"],
    )
    # Fields with defaulted/validated construction above are now overwritten
    # with the exact persisted values (status, timestamps, computed P&L, etc).
    trade.status = TradeStatus(row["status"])
    trade.trailing_stop_price = _money_or_none(row["trailing_stop_price"], currency)
    trade.exit_price = _money_or_none(row["exit_price"], currency)
    trade.realized_pnl = _money_or_none(row["realized_pnl"], currency)
    trade.target_hit = row["target_hit"]
    trade.notes = row["notes"] or ""
    trade.opened_at = datetime.fromisoformat(row["opened_at"])
    trade.closed_at = datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None
    trade.created_at = datetime.fromisoformat(row["created_at"])
    trade.updated_at = datetime.fromisoformat(row["updated_at"])
    return trade


class SQLiteTradeRepository(TradeRepository):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def save(self, trade: Trade) -> None:
        currency = trade.entry_price.currency
        self._conn.execute(
            """
            INSERT INTO trades (
                id, symbol, side, entry_price, quantity, currency,
                stop_loss, target_1, target_2, target_3, target_hit,
                trailing_stop_distance, trailing_stop_price,
                status, exit_price, realized_pnl, notes,
                opened_at, closed_at, created_at, updated_at
            ) VALUES (
                :id, :symbol, :side, :entry_price, :quantity, :currency,
                :stop_loss, :target_1, :target_2, :target_3, :target_hit,
                :trailing_stop_distance, :trailing_stop_price,
                :status, :exit_price, :realized_pnl, :notes,
                :opened_at, :closed_at, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                symbol=excluded.symbol,
                side=excluded.side,
                entry_price=excluded.entry_price,
                quantity=excluded.quantity,
                currency=excluded.currency,
                stop_loss=excluded.stop_loss,
                target_1=excluded.target_1,
                target_2=excluded.target_2,
                target_3=excluded.target_3,
                target_hit=excluded.target_hit,
                trailing_stop_distance=excluded.trailing_stop_distance,
                trailing_stop_price=excluded.trailing_stop_price,
                status=excluded.status,
                exit_price=excluded.exit_price,
                realized_pnl=excluded.realized_pnl,
                notes=excluded.notes,
                opened_at=excluded.opened_at,
                closed_at=excluded.closed_at,
                updated_at=excluded.updated_at
            """,
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side.value,
                "entry_price": str(trade.entry_price.amount),
                "quantity": str(trade.quantity),
                "currency": currency,
                "stop_loss": _text_or_none(trade.stop_loss),
                "target_1": _text_or_none(trade.target_1),
                "target_2": _text_or_none(trade.target_2),
                "target_3": _text_or_none(trade.target_3),
                "target_hit": trade.target_hit,
                "trailing_stop_distance": _text_or_none(trade.trailing_stop_distance),
                "trailing_stop_price": _text_or_none(trade.trailing_stop_price),
                "status": trade.status.value,
                "exit_price": _text_or_none(trade.exit_price),
                "realized_pnl": _text_or_none(trade.realized_pnl),
                "notes": trade.notes,
                "opened_at": trade.opened_at.isoformat(),
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
                "created_at": trade.created_at.isoformat(),
                "updated_at": trade.updated_at.isoformat(),
            },
        )
        self._conn.commit()

    def get_by_id(self, trade_id: str) -> Trade | None:
        row = self._conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return _row_to_trade(row) if row else None

    def list_all(self) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_trade(row) for row in rows]

    def list_by_status(self, status: TradeStatus) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status = ? ORDER BY created_at DESC",
            (status.value,),
        ).fetchall()
        return [_row_to_trade(row) for row in rows]

    def list_by_symbol(self, symbol: str) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE symbol = ? ORDER BY created_at DESC",
            (symbol,),
        ).fetchall()
        return [_row_to_trade(row) for row in rows]
