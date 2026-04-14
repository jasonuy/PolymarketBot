import os
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket API ────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

PRIVATE_KEY      = os.getenv("PRIVATE_KEY", "")
POLY_API_KEY     = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET  = os.getenv("POLY_API_SECRET", "")
POLY_API_PASS    = os.getenv("POLY_API_PASSPHRASE", "")
WALLET_ADDRESS   = os.getenv("WALLET_ADDRESS", "").lower()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Bot behavior ──────────────────────────────────────────────────────────────
PAPER_TRADE      = os.getenv("PAPER_TRADE", "true").lower() == "true"
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# ── Risk management ───────────────────────────────────────────────────────────
MAX_POSITION_FRACTION = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
MAX_TRADE_USDC        = float(os.getenv("MAX_TRADE_USDC",        "50.0"))
MIN_WHALE_TRADE_USDC  = float(os.getenv("MIN_WHALE_TRADE_USDC",  "5.0"))
MAX_OPEN_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS",       "10"))
MAX_SPREAD_PCT        = float(os.getenv("MAX_SPREAD_PCT",         "0.10"))

# ── Exit thresholds ───────────────────────────────────────────────────────────
# Close a position early if price moves this far from entry (as a fraction).
# e.g. 0.50 = close if price drops 50% from entry (stop-loss)
#      0.80 = close if price rises 80% from entry (take-profit)
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "0.50"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.80"))

# ── Wallet performance filtering ─────────────────────────────────────────────
# After MIN_TRADES_BEFORE_FILTER closed trades from a wallet, skip future signals
# if the wallet's win rate falls below MIN_WIN_RATE_TO_FOLLOW.
MIN_TRADES_BEFORE_FILTER = int(os.getenv("MIN_TRADES_BEFORE_FILTER", "5"))
MIN_WIN_RATE_TO_FOLLOW   = float(os.getenv("MIN_WIN_RATE_TO_FOLLOW",  "0.45"))

# ── Wallets to follow ─────────────────────────────────────────────────────────
# Add wallet addresses you want to copy-trade.
# Find top traders at polymarket.com/leaderboard
WATCHED_WALLETS = [
    # Example known profitable wallets — replace or add your own
    # "0xabc123...",
    # "0xdef456...",
]

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = "polymarket_bot.db"
