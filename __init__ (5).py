"""SQLite implementation of the PortfolioRepository port."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from titan_ai_trader.application.interfaces.portfolio_repository import PortfolioRepository
from titan_ai_trader.domain.entities.portfolio import Portfolio
from titan_ai_trader.domain.value_objects.money import Money


def _row_to_portfolio(row: sqlite3.Row) -> Portfolio:
    currency = row["currency"]
    portfolio = Portfolio(
        name=row["name"],
        starting_cash=Money.of(row["starting_cash"], currency),
        realized_pnl=Money.of(row["realized_pnl"], currency),
        id=row["id"],
    )
    portfolio.cash = Money.of(row["cash"], currency)
    portfolio.created_at = datetime.fromisoformat(row["created_at"])
    portfolio.updated_at = datetime.fromisoformat(row["updated_at"])
    return portfolio


class SQLitePortfolioRepository(PortfolioRepository):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def save(self, portfolio: Portfolio) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolios (
                id, name, starting_cash, cash, realized_pnl, currency,
                created_at, updated_at
            ) VALUES (
                :id, :name, :starting_cash, :cash, :realized_pnl, :currency,
                :created_at, :updated_at
            )
            ON CONFLICT(name) DO UPDATE SET
                cash=excluded.cash,
                realized_pnl=excluded.realized_pnl,
                updated_at=excluded.updated_at
            """,
            {
                "id": portfolio.id,
                "name": portfolio.name,
                "starting_cash": str(portfolio.starting_cash.amount),
                "cash": str(portfolio.cash.amount),
                "realized_pnl": str(portfolio.realized_pnl.amount),
                "currency": portfolio.cash.currency,
                "created_at": portfolio.created_at.isoformat(),
                "updated_at": portfolio.updated_at.isoformat(),
            },
        )
        self._conn.commit()

    def get_by_name(self, name: str) -> Portfolio | None:
        row = self._conn.execute(
            "SELECT * FROM portfolios WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_portfolio(row) if row else None

    def list_all(self) -> list[Portfolio]:
        rows = self._conn.execute("SELECT * FROM portfolios").fetchall()
        return [_row_to_portfolio(row) for row in rows]
