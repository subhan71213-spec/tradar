"""SQLite schema definitions for the paper trading engine.

Kept as plain SQL DDL (no ORM) so Phase 1 has zero third-party
dependencies. All monetary values are stored as TEXT holding a decimal
string (not FLOAT) to avoid floating point rounding errors; they are
parsed back into Decimal/Money at the repository boundary.
"""

from __future__ import annotations

import sqlite3

CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id                      TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    side                    TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    entry_price             TEXT NOT NULL,
    quantity                TEXT NOT NULL,
    currency                TEXT NOT NULL DEFAULT 'USD',

    stop_loss               TEXT,
    target_1                TEXT,
    target_2                TEXT,
    target_3                TEXT,
    target_hit              INTEGER,

    trailing_stop_distance  TEXT,
    trailing_stop_price     TEXT,

    status                  TEXT NOT NULL CHECK (
        status IN ('PENDING', 'OPEN', 'CLOSED', 'STOPPED_OUT', 'TARGET_HIT', 'CANCELLED')
    ),
    exit_price               TEXT,
    realized_pnl              TEXT,

    notes                    TEXT NOT NULL DEFAULT '',

    opened_at                TEXT NOT NULL,
    closed_at                TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
"""

CREATE_POSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS positions (
    id                      TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    side                    TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    quantity                TEXT NOT NULL,
    average_entry_price     TEXT NOT NULL,
    currency                TEXT NOT NULL DEFAULT 'USD',
    realized_pnl            TEXT NOT NULL DEFAULT '0',
    trade_ids                TEXT NOT NULL DEFAULT '[]',
    opened_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    UNIQUE(symbol)
);
"""

CREATE_PORTFOLIOS_TABLE = """
CREATE TABLE IF NOT EXISTS portfolios (
    id                      TEXT PRIMARY KEY,
    name                     TEXT NOT NULL UNIQUE,
    starting_cash             TEXT NOT NULL,
    cash                     TEXT NOT NULL,
    realized_pnl              TEXT NOT NULL DEFAULT '0',
    currency                 TEXT NOT NULL DEFAULT 'USD',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
"""

CREATE_JOURNAL_ENTRIES_TABLE = """
CREATE TABLE IF NOT EXISTS journal_entries (
    id                      TEXT PRIMARY KEY,
    content                  TEXT NOT NULL,
    trade_id                 TEXT,
    tags                     TEXT NOT NULL DEFAULT '[]',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_journal_trade_id ON journal_entries(trade_id);
"""

ALL_STATEMENTS = (
    CREATE_TRADES_TABLE,
    CREATE_POSITIONS_TABLE,
    CREATE_PORTFOLIOS_TABLE,
    CREATE_JOURNAL_ENTRIES_TABLE,
    CREATE_INDEXES,
)


def init_db(connection: sqlite3.Connection) -> None:
    """Create all Phase 1 tables/indexes if they do not already exist."""
    cursor = connection.cursor()
    for statement in ALL_STATEMENTS:
        cursor.executescript(statement)
    connection.commit()
