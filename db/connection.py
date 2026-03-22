"""
db/connection.py

Responsibilities:
  - Open (or create) the SQLite database file
  - Run schema.sql once to create all tables and indexes
  - Provide a single shared connection to every module that imports this

Why a module-level singleton?
  SQLite on a single file supports multiple readers but only one writer at a
  time. Since this is a single-process bot (one asyncio event loop, no
  multiprocessing), sharing one connection is safe and avoids the overhead
  of opening/closing per call. The connection is opened in check_same_thread=False
  so it can be shared across asyncio tasks running on the same thread.

Input:  DB_PATH from environment (e.g. "asoka.db") — set before calling init()
Output: a sqlite3.Connection object available via get_connection()

Usage:
    from db.connection import init, get_connection

    init("asoka.db")          # call once at startup
    conn = get_connection()   # call anywhere, returns the same connection
"""

import sqlite3
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level singleton — one connection for the lifetime of the process
_connection: sqlite3.Connection | None = None

# Path to schema.sql is relative to this file, so it works regardless of cwd
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init(db_path: str) -> None:
    """
    Open the SQLite file at db_path and execute schema.sql.

    CREATE TABLE IF NOT EXISTS in schema.sql makes this idempotent:
    calling init() on an existing database is safe — it adds no tables
    and changes no data.

    Args:
        db_path: file path for the SQLite database, e.g. "asoka.db"
    """
    global _connection

    logger.info(f"Initializing database at: {os.path.abspath(db_path)}")

    _connection = sqlite3.connect(
        db_path,
        check_same_thread=False,  # safe for single-process asyncio
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        # PARSE_DECLTYPES: lets sqlite3 auto-convert TIMESTAMP columns to
        # Python datetime objects when reading rows
    )

    # Return rows as dict-like objects so callers can do row["batch_id"]
    # instead of row[0] — much safer with evolving schemas
    _connection.row_factory = sqlite3.Row

    # Enforce foreign key constraints (SQLite has them OFF by default)
    _connection.execute("PRAGMA foreign_keys = ON")

    # WAL mode: readers don't block writers and vice versa.
    # Better for the async pattern where reads and writes can interleave.
    _connection.execute("PRAGMA journal_mode = WAL")

    _connection.commit()

    # Create all tables and indexes defined in schema.sql
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    _connection.executescript(schema_sql)
    # executescript() implicitly commits — no explicit commit needed here

    logger.info("Database initialized. All tables and indexes are ready.")


def get_connection() -> sqlite3.Connection:
    """
    Return the shared connection.

    Raises RuntimeError if init() was never called — this surfaces
    a startup ordering bug immediately rather than silently failing later.
    """
    if _connection is None:
        raise RuntimeError(
            "Database not initialized. Call db.connection.init(db_path) "
            "before using get_connection()."
        )
    return _connection


def close() -> None:
    """
    Close the connection cleanly.
    Called during graceful shutdown (main.py teardown).
    """
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
        logger.info("Database connection closed.")
