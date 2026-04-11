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
# Maximum fraction of bankroll to risk on a single trade (Kelly-inspired)
MAX_POSITION_FRACTION = 0.05       # 5% of bankroll per trade

# Absolute max USDC per single trade (hard cap)
MAX_TRADE_USDC = 50.0

# Minimum USDC value of a whale trade before we consider copying it
MIN_WHALE_TRADE_USDC = 100.0

# Maximum number of open positions at once
MAX_OPEN_POSITIONS = 10

# Minimum market liquidity (spread %) to enter — avoids illiquid traps
MAX_SPREAD_PCT = 0.10              # skip if bid/ask spread > 10%

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
