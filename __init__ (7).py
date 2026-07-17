"""SQLite connection factory.

Single place that knows how to open a connection to the paper trading
database. Enables foreign keys and returns rows as sqlite3.Row so
repositories can access columns by name.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from titan_ai_trader.infrastructure.persistence.db.schema import init_db


def _connect(database_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def get_engine(database_path: Path | str, *, ensure_schema: bool = True) -> sqlite3.Connection:
    """Return a live, schema-initialized connection.

    Kept simple (single long-lived connection) for Phase 1. A connection
    pool is unnecessary for a local paper trading engine.
    """
    connection = _connect(database_path)
    if ensure_schema:
        init_db(connection)
    return connection


@contextmanager
def session_scope(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and rolls back on error."""
    connection = get_engine(database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
