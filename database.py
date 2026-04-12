"""
SQLite persistence layer.
Tracks detected whale trades, our copy trades, open positions, and P&L.
"""

import sqlite3
import logging
from datetime import datetime, UTC
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS whale_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at     TEXT    NOT NULL,
                wallet          TEXT    NOT NULL,
                market_id       TEXT    NOT NULL,
                market_question TEXT,
                outcome         TEXT    NOT NULL,
                side            TEXT    NOT NULL,   -- BUY / SELL
                price           REAL    NOT NULL,
                size_usdc       REAL    NOT NULL,
                tx_hash         TEXT
            );

            CREATE TABLE IF NOT EXISTS copy_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                whale_trade_id  INTEGER REFERENCES whale_trades(id),
                placed_at       TEXT    NOT NULL,
                market_id       TEXT    NOT NULL,
                market_question TEXT,
                token_id        TEXT,
                outcome         TEXT    NOT NULL,
                side            TEXT    NOT NULL,
                price           REAL    NOT NULL,
                size_usdc       REAL    NOT NULL,
                order_id        TEXT,
                paper_trade     INTEGER NOT NULL DEFAULT 1,  -- 1=paper, 0=live
                status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN/CLOSED/CANCELLED
                closed_at       TEXT,
                close_price     REAL,
                pnl_usdc        REAL
            );

            CREATE TABLE IF NOT EXISTS seen_tx (
                tx_hash TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            );
        """)
    # Migrate existing databases: add columns if they don't exist yet
    with get_conn() as conn:
        for col_def in [
            ("copy_trades", "market_question", "TEXT"),
            ("copy_trades", "token_id",        "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]} {col_def[2]}")
            except sqlite3.OperationalError:
                pass  # column already exists

    logger.info("Database initialised at %s", DB_PATH)


def record_whale_trade(wallet: str, market_id: str, market_question: str,
                       outcome: str, side: str, price: float,
                       size_usdc: float, tx_hash: str = "") -> int:
    """Insert a detected whale trade. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO whale_trades
               (detected_at, wallet, market_id, market_question, outcome, side, price, size_usdc, tx_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(UTC).isoformat(), wallet, market_id, market_question,
             outcome, side, price, size_usdc, tx_hash)
        )
        return cur.lastrowid


def record_copy_trade(whale_trade_id: int, market_id: str, outcome: str,
                      side: str, price: float, size_usdc: float,
                      order_id: str = "", paper_trade: bool = True,
                      token_id: str = "", market_question: str = "") -> int:
    """Insert a copy trade record. Returns the new row id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO copy_trades
               (whale_trade_id, placed_at, market_id, market_question, token_id,
                outcome, side, price, size_usdc, order_id, paper_trade)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (whale_trade_id, datetime.now(UTC).isoformat(), market_id, market_question,
             token_id, outcome, side, price, size_usdc, order_id, 1 if paper_trade else 0)
        )
        return cur.lastrowid


def close_trade(copy_trade_id: int, close_price: float, pnl_usdc: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE copy_trades
               SET status='CLOSED', closed_at=?, close_price=?, pnl_usdc=?
               WHERE id=?""",
            (datetime.now(UTC).isoformat(), close_price, pnl_usdc, copy_trade_id)
        )


def is_tx_seen(tx_hash: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_tx WHERE tx_hash=?", (tx_hash,)).fetchone()
        return row is not None


def mark_tx_seen(tx_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_tx (tx_hash, seen_at) VALUES (?, ?)",
            (tx_hash, datetime.now(UTC).isoformat())
        )


def get_open_positions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_trades WHERE status='OPEN'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_pnl_summary() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END), 0) as closed,
                COALESCE(SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END), 0) as open,
                ROUND(COALESCE(SUM(COALESCE(pnl_usdc, 0)), 0), 4) as total_pnl
               FROM copy_trades"""
        ).fetchone()
        return dict(row) if row else {"total_trades": 0, "closed": 0, "open": 0, "total_pnl": 0.0}
