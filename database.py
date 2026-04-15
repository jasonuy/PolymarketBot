"""
SQLite persistence layer.
Tracks detected whale trades, our copy trades, open positions, P&L, and
daily stats snapshots for nightly optimization sessions.
"""

import json
import os
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

            CREATE TABLE IF NOT EXISTS wallet_stats (
                wallet          TEXT PRIMARY KEY,
                total_copies    INTEGER NOT NULL DEFAULT 0,
                wins            INTEGER NOT NULL DEFAULT 0,
                losses          INTEGER NOT NULL DEFAULT 0,
                total_pnl       REAL    NOT NULL DEFAULT 0.0,
                last_updated    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date        TEXT PRIMARY KEY,   -- YYYY-MM-DD UTC
                stats_json  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
        """)
    # Migrate existing databases: add columns if they don't exist yet
    with get_conn() as conn:
        for col_def in [
            ("copy_trades", "market_question", "TEXT"),
            ("copy_trades", "token_id",        "TEXT"),
            ("copy_trades", "close_reason",    "TEXT"),
            ("copy_trades", "event_type",      "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]} {col_def[2]}")
            except sqlite3.OperationalError:
                pass  # column already exists

    logger.info("Database initialised at %s", DB_PATH)


# ── Event classification ──────────────────────────────────────────────────────

_NBA = [
    "celtics","lakers","knicks","bulls","warriors","nets","heat","bucks","sixers",
    "raptors","nuggets","suns","clippers","pistons","pacers","hornets","magic",
    "hawks","wizards","cavaliers","thunder","blazers","trail blazers","kings",
    "jazz","timberwolves","grizzlies","pelicans","rockets","spurs","mavericks",
    "mavs","nba","spread:","basketball",
]
_MLB = [
    "yankees","red sox","dodgers","cubs","cardinals","mets","giants","braves",
    "astros","white sox","rangers","athletics","mariners","royals","tigers",
    "twins","guardians","orioles","rays","blue jays","phillies","nationals",
    "marlins","reds","brewers","pirates","rockies","padres","angels",
    "diamondbacks","mlb","baseball",
]
_NHL = [
    "bruins","maple leafs","canadiens","flyers","penguins","capitals",
    "hurricanes","panthers","lightning","senators","sabres","red wings",
    "blue jackets","islanders","devils","blackhawks","blues","predators",
    "jets","wild","avalanche","flames","oilers","canucks","sharks","ducks",
    "golden knights","kraken","stars","nhl","hockey",
]
_NFL = [
    "patriots","chiefs","eagles","cowboys","49ers","packers","steelers",
    "ravens","seahawks","broncos","bills","dolphins","raiders","chargers",
    "colts","titans","jaguars","texans","bengals","browns","lions","vikings",
    "bears","falcons","saints","buccaneers","cardinals","rams","commanders",
    "nfl","super bowl","football",
]
_SOCCER = [
    "fc ","premier league","champions league","la liga","bundesliga","serie a",
    "ligue 1","mls","fluminense","cruzeiro","bragantino","flamengo","vasco",
    "palmeiras","atletico","internazionale","inter milan","o/u 2.5","o/u 1.5",
    "o/u 3.5","soccer","football club",
]
_POLITICS = [
    "election","president","congress","senate","house","vote","ballot",
    "democrat","republican","trump","biden","harris","governor","mayor",
    "referendum","poll","approval",
]
_CRYPTO = [
    "bitcoin","btc","ethereum","eth","solana","sol","crypto","usdc","usdt",
    "binance","coinbase","defi","nft","token","blockchain",
]


def classify_event_type(question: str) -> str:
    """Infer an event category from a market question string."""
    q = (question or "").lower()
    if any(k in q for k in _NBA):
        return "NBA"
    if any(k in q for k in _MLB):
        return "MLB"
    if any(k in q for k in _NHL):
        return "NHL"
    if any(k in q for k in _NFL):
        return "NFL"
    if any(k in q for k in _SOCCER):
        return "Soccer"
    if any(k in q for k in _POLITICS):
        return "Politics"
    if any(k in q for k in _CRYPTO):
        return "Crypto"
    if any(k in q for k in ["win on","vs.","match","tournament","championship",
                              "series","playoff","game 1","game 2","game 3"]):
        return "Sports-Other"
    return "Other"


# ── Core write functions ──────────────────────────────────────────────────────

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
    event_type = classify_event_type(market_question)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO copy_trades
               (whale_trade_id, placed_at, market_id, market_question, token_id,
                outcome, side, price, size_usdc, order_id, paper_trade, event_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (whale_trade_id, datetime.now(UTC).isoformat(), market_id, market_question,
             token_id, outcome, side, price, size_usdc, order_id,
             1 if paper_trade else 0, event_type)
        )
        return cur.lastrowid


def close_trade(copy_trade_id: int, close_price: float, pnl_usdc: float,
                close_reason: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE copy_trades
               SET status='CLOSED', closed_at=?, close_price=?, pnl_usdc=?, close_reason=?
               WHERE id=?""",
            (datetime.now(UTC).isoformat(), close_price, pnl_usdc,
             close_reason, copy_trade_id)
        )


def cancel_trade(copy_trade_id: int, reason: str = "") -> None:
    """Mark a trade as CANCELLED (order was never filled or was rejected)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE copy_trades
               SET status='CANCELLED', closed_at=?, close_reason=?
               WHERE id=?""",
            (datetime.now(UTC).isoformat(), reason, copy_trade_id)
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
            """SELECT ct.*, wt.wallet AS source_wallet
               FROM copy_trades ct
               LEFT JOIN whale_trades wt ON ct.whale_trade_id = wt.id
               WHERE ct.status='OPEN'"""
        ).fetchall()
        return [dict(r) for r in rows]


def update_wallet_stats(wallet: str, pnl: float) -> None:
    """Increment the win/loss record for a wallet after a trade closes."""
    win  = 1 if pnl > 0 else 0
    loss = 1 if pnl <= 0 else 0
    now  = datetime.now(UTC).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT total_copies, wins, losses, total_pnl FROM wallet_stats WHERE wallet=?",
            (wallet,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE wallet_stats
                   SET total_copies=?, wins=?, losses=?, total_pnl=?, last_updated=?
                   WHERE wallet=?""",
                (existing["total_copies"] + 1,
                 existing["wins"] + win,
                 existing["losses"] + loss,
                 round(existing["total_pnl"] + pnl, 2),
                 now, wallet)
            )
        else:
            conn.execute(
                """INSERT INTO wallet_stats
                   (wallet, total_copies, wins, losses, total_pnl, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (wallet, 1, win, loss, round(pnl, 2), now)
            )


def get_wallet_stats(wallet: str) -> dict | None:
    """Returns closed-trade stats for a wallet, or None if not seen before."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallet_stats WHERE wallet=?", (wallet,)
        ).fetchone()
        return dict(row) if row else None


def get_all_wallet_stats() -> list[dict]:
    """Returns stats for all tracked wallets, worst performers first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_stats ORDER BY total_pnl ASC"
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


# ── Stats report (for nightly optimization) ───────────────────────────────────

def _pct(wins: int, total: int) -> float:
    return round(wins / total, 4) if total > 0 else 0.0


def get_stats_report() -> dict:
    """
    Returns a comprehensive stats dict for nightly optimization.
    Covers: overall, per-whale, per-event-type, per-price-range,
    per-trade-size, exit-reason breakdown, daily trend, and current config.
    """
    from config import (
        STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_OPEN_POSITIONS,
        MAX_TRADE_USDC, MIN_WHALE_TRADE_USDC, MAX_POSITION_FRACTION,
        MAX_SPREAD_PCT, MIN_WIN_RATE_TO_FOLLOW, MIN_TRADES_BEFORE_FILTER,
        POLL_INTERVAL, PAPER_TRADE,
    )

    with get_conn() as conn:

        # ── Overall ───────────────────────────────────────────────────────────
        ov = conn.execute("""
            SELECT
                COUNT(*)                                              AS total,
                SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END)     AS open,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END)     AS closed,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc > 0
                         THEN 1 ELSE 0 END)                          AS wins,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc <= 0
                         THEN 1 ELSE 0 END)                          AS losses,
                ROUND(SUM(COALESCE(pnl_usdc, 0)), 2)                 AS total_pnl,
                ROUND(AVG(CASE WHEN status='CLOSED'
                          THEN pnl_usdc END), 2)                     AS avg_pnl_per_trade,
                ROUND(AVG(CASE WHEN status='CLOSED' AND pnl_usdc > 0
                          THEN pnl_usdc END), 2)                     AS avg_win,
                ROUND(AVG(CASE WHEN status='CLOSED' AND pnl_usdc <= 0
                          THEN pnl_usdc END), 2)                     AS avg_loss
            FROM copy_trades
        """).fetchone()
        overall = dict(ov)
        overall["win_rate"] = _pct(overall["wins"] or 0, overall["closed"] or 0)

        # ── Per whale ─────────────────────────────────────────────────────────
        by_whale = []
        for r in conn.execute("""
            SELECT
                wt.wallet,
                COUNT(*)                                             AS total,
                SUM(CASE WHEN ct.status='CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN ct.status='CLOSED' AND ct.pnl_usdc > 0
                         THEN 1 ELSE 0 END)                         AS wins,
                SUM(CASE WHEN ct.status='CLOSED' AND ct.pnl_usdc <= 0
                         THEN 1 ELSE 0 END)                         AS losses,
                SUM(CASE WHEN ct.status='OPEN'   THEN 1 ELSE 0 END) AS open,
                ROUND(SUM(COALESCE(ct.pnl_usdc, 0)), 2)             AS total_pnl,
                ROUND(AVG(CASE WHEN ct.status='CLOSED'
                          THEN ct.pnl_usdc END), 2)                 AS avg_pnl,
                ROUND(AVG(ct.size_usdc), 2)                         AS avg_size
            FROM copy_trades ct
            JOIN whale_trades wt ON ct.whale_trade_id = wt.id
            GROUP BY wt.wallet
            ORDER BY total_pnl ASC
        """).fetchall():
            d = dict(r)
            d["win_rate"] = _pct(d["wins"] or 0, d["closed"] or 0)
            by_whale.append(d)

        # ── Per event type ────────────────────────────────────────────────────
        by_event = []
        for r in conn.execute("""
            SELECT
                COALESCE(event_type, 'Other')                        AS event_type,
                COUNT(*)                                             AS total,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END)    AS closed,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc > 0
                         THEN 1 ELSE 0 END)                         AS wins,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc <= 0
                         THEN 1 ELSE 0 END)                         AS losses,
                ROUND(SUM(COALESCE(pnl_usdc, 0)), 2)                AS total_pnl,
                ROUND(AVG(CASE WHEN status='CLOSED'
                          THEN pnl_usdc END), 2)                    AS avg_pnl
            FROM copy_trades
            GROUP BY event_type
            ORDER BY total_pnl ASC
        """).fetchall():
            d = dict(r)
            d["win_rate"] = _pct(d["wins"] or 0, d["closed"] or 0)
            by_event.append(d)

        # ── Per entry price range ─────────────────────────────────────────────
        by_price = []
        for r in conn.execute("""
            SELECT
                CASE
                    WHEN price < 0.10 THEN '0.00-0.10 (longshot)'
                    WHEN price < 0.25 THEN '0.10-0.25 (unlikely)'
                    WHEN price < 0.45 THEN '0.25-0.45 (underdog)'
                    WHEN price < 0.55 THEN '0.45-0.55 (coinflip)'
                    WHEN price < 0.75 THEN '0.55-0.75 (favourite)'
                    WHEN price < 0.90 THEN '0.75-0.90 (likely)'
                    ELSE                   '0.90-1.00 (near-cert)'
                END AS price_range,
                COUNT(*)                                             AS total,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END)    AS closed,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc > 0
                         THEN 1 ELSE 0 END)                         AS wins,
                SUM(CASE WHEN status='CLOSED' AND pnl_usdc <= 0
                         THEN 1 ELSE 0 END)                         AS losses,
                ROUND(SUM(COALESCE(pnl_usdc, 0)), 2)                AS total_pnl,
                ROUND(AVG(CASE WHEN status='CLOSED'
                          THEN pnl_usdc END), 2)                    AS avg_pnl
            FROM copy_trades
            GROUP BY price_range
            ORDER BY price ASC
        """).fetchall():
            d = dict(r)
            d["win_rate"] = _pct(d["wins"] or 0, d["closed"] or 0)
            by_price.append(d)

        # ── Per whale trade size ──────────────────────────────────────────────
        by_size = []
        for r in conn.execute("""
            SELECT
                CASE
                    WHEN wt.size_usdc <   50 THEN '< $50'
                    WHEN wt.size_usdc <  200 THEN '$50-200'
                    WHEN wt.size_usdc <  500 THEN '$200-500'
                    WHEN wt.size_usdc < 1000 THEN '$500-1k'
                    WHEN wt.size_usdc < 5000 THEN '$1k-5k'
                    ELSE                          '$5k+'
                END AS whale_size_range,
                COUNT(*)                                             AS total,
                SUM(CASE WHEN ct.status='CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN ct.status='CLOSED' AND ct.pnl_usdc > 0
                         THEN 1 ELSE 0 END)                         AS wins,
                SUM(CASE WHEN ct.status='CLOSED' AND ct.pnl_usdc <= 0
                         THEN 1 ELSE 0 END)                         AS losses,
                ROUND(SUM(COALESCE(ct.pnl_usdc, 0)), 2)             AS total_pnl,
                ROUND(AVG(wt.size_usdc), 0)                         AS avg_whale_size
            FROM copy_trades ct
            JOIN whale_trades wt ON ct.whale_trade_id = wt.id
            GROUP BY whale_size_range
            ORDER BY wt.size_usdc ASC
        """).fetchall():
            d = dict(r)
            d["win_rate"] = _pct(d["wins"] or 0, d["closed"] or 0)
            by_size.append(d)

        # ── Exit reason breakdown ─────────────────────────────────────────────
        by_exit = []
        for r in conn.execute("""
            SELECT
                CASE
                    WHEN close_reason LIKE 'Stop-loss%'       THEN 'Stop-loss'
                    WHEN close_reason LIKE 'Take-profit%'     THEN 'Take-profit'
                    WHEN close_reason LIKE '%WIN%'            THEN 'Resolved WIN'
                    WHEN close_reason LIKE '%LOSS%'           THEN 'Resolved LOSS'
                    WHEN close_reason IS NULL OR close_reason = ''
                                                              THEN 'Unknown'
                    ELSE close_reason
                END AS exit_reason,
                COUNT(*)                                        AS count,
                ROUND(SUM(COALESCE(pnl_usdc, 0)), 2)           AS total_pnl,
                ROUND(AVG(pnl_usdc), 2)                        AS avg_pnl
            FROM copy_trades
            WHERE status = 'CLOSED'
            GROUP BY exit_reason
            ORDER BY count DESC
        """).fetchall():
            by_exit.append(dict(r))

        # ── Daily trend (last 30 days) ────────────────────────────────────────
        daily_trend = []
        for r in conn.execute("""
            SELECT
                SUBSTR(closed_at, 1, 10)                             AS date,
                COUNT(*)                                             AS closed,
                SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END)       AS wins,
                SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END)      AS losses,
                ROUND(SUM(pnl_usdc), 2)                              AS pnl,
                ROUND(AVG(pnl_usdc), 2)                              AS avg_pnl
            FROM copy_trades
            WHERE status = 'CLOSED' AND closed_at IS NOT NULL
            GROUP BY date
            ORDER BY date DESC
            LIMIT 30
        """).fetchall():
            d = dict(r)
            d["win_rate"] = _pct(d["wins"] or 0, d["closed"] or 0)
            daily_trend.append(d)

        # ── Open positions summary ────────────────────────────────────────────
        open_positions = []
        for r in conn.execute("""
            SELECT ct.market_question, ct.outcome, ct.price AS entry_price,
                   ct.size_usdc, ct.placed_at,
                   COALESCE(ct.event_type, 'Other') AS event_type,
                   wt.wallet
            FROM copy_trades ct
            LEFT JOIN whale_trades wt ON ct.whale_trade_id = wt.id
            WHERE ct.status = 'OPEN'
            ORDER BY ct.placed_at ASC
        """).fetchall():
            open_positions.append(dict(r))

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "overall": overall,
        "by_whale": by_whale,
        "by_event_type": by_event,
        "by_entry_price_range": by_price,
        "by_whale_trade_size": by_size,
        "by_exit_reason": by_exit,
        "daily_trend": daily_trend,
        "open_positions": open_positions,
        "config": {
            "PAPER_TRADE":             PAPER_TRADE,
            "POLL_INTERVAL":           POLL_INTERVAL,
            "STOP_LOSS_PCT":           STOP_LOSS_PCT,
            "TAKE_PROFIT_PCT":         TAKE_PROFIT_PCT,
            "MAX_OPEN_POSITIONS":      MAX_OPEN_POSITIONS,
            "MAX_TRADE_USDC":          MAX_TRADE_USDC,
            "MIN_WHALE_TRADE_USDC":    MIN_WHALE_TRADE_USDC,
            "MAX_POSITION_FRACTION":   MAX_POSITION_FRACTION,
            "MAX_SPREAD_PCT":          MAX_SPREAD_PCT,
            "MIN_WIN_RATE_TO_FOLLOW":  MIN_WIN_RATE_TO_FOLLOW,
            "MIN_TRADES_BEFORE_FILTER": MIN_TRADES_BEFORE_FILTER,
        },
    }
    return report


def snapshot_daily_stats() -> None:
    """
    Save today's full stats report to the daily_stats table and to
    stats_snapshot.json next to the database file.
    Called once per calendar day by the bot.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    report = get_stats_report()
    report_json = json.dumps(report, indent=2)
    now = datetime.now(UTC).isoformat()

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO daily_stats (date, stats_json, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET stats_json=excluded.stats_json,
                                               created_at=excluded.created_at""",
            (today, report_json, now)
        )

    # Also write a flat JSON file alongside the DB for easy access
    snapshot_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)),
                                 "stats_snapshot.json")
    with open(snapshot_path, "w") as f:
        f.write(report_json)

    logger.info("Daily stats snapshot saved → %s", snapshot_path)
