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
FUNDER_ADDRESS   = os.getenv("FUNDER_ADDRESS", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Bot behavior ──────────────────────────────────────────────────────────────
PAPER_TRADE      = os.getenv("PAPER_TRADE", "true").lower() == "true"
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# ── Risk management ───────────────────────────────────────────────────────────
MAX_POSITION_FRACTION = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
MAX_TRADE_USDC        = float(os.getenv("MAX_TRADE_USDC",        "50.0"))
# Set LIVE_BANKROLL in .env to your Polymarket Cash balance.
# The bot uses this for position sizing instead of querying the CLOB.
LIVE_BANKROLL         = float(os.getenv("LIVE_BANKROLL",         "0.0"))
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
MAX_POSITIONS_PER_WALLET = int(os.getenv("MAX_POSITIONS_PER_WALLET",  "2"))

# ── Dynamic whale trust scoring ───────────────────────────────────────────────
# New wallets start with INITIAL_TRUST_LEVEL concurrent-position slots.
# Each WIN earns +1 slot (up to MAX_TRUST_LEVEL); each LOSS costs -1.
# At 0 the wallet is blacklisted — no further copies until manually reset.
INITIAL_TRUST_LEVEL = int(os.getenv("INITIAL_TRUST_LEVEL", "3"))
MAX_TRUST_LEVEL     = int(os.getenv("MAX_TRUST_LEVEL",     "10"))

# ── Wallets to follow ─────────────────────────────────────────────────────────
# Mixed list: top sports traders (Polymarket leaderboard) + political specialists (PolySmartWallet)
# Updated 2026-04-14 based on cross-leaderboard analysis
WATCHED_WALLETS = [
    # ── Sports specialists (Polymarket top-20 by monthly PnL) ─────────────────
    "0x492442eab586f242b53bda933fd5de859c8a3782",  # #1  anon          $6.5M/mo
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",  # #2  HorizonSplendidView  $4.0M/mo
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",  # #3  reachingthesky  $3.7M/mo
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",  # #4  beachboy4      $2.7M/mo
    "0x019782cab5d844f02bafb71f512758be78579f3c",  # #5  majorexploiter  $2.4M/mo
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",  # #6  RN1            $2.1M/mo
    "0xbddf61af533ff524d27154e589d2d7a81510c684",  # #7  Countryside    $1.9M/mo
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",  # #8  anon           $1.9M/mo
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",  # #9  sovereign2013  $1.8M/mo
    "0x93abbc022ce98d6f45d4444b594791cc4b7a9723",  # #15 gatorr         $1.3M/mo
    "0xdc876e6873772d38716fda7f2452a78d426d7ab6",  # #11 anon           $1.5M/mo
    # ── Political/macro specialists (PolySmartWallet top score, non-sports) ───
    "0x5da55a322ba9099b2910780c0bf84c7afafd56ac",  # GBVT43TY   91.5% win, 93% politics
    "0xfbaaf6df48ac710f7e4f714a6e8c710e30fc1869",  # GBVTD455   91.3% win, 93% politics
    "0x65e53eb81aa01db5cf9cd8a631d104f2aeed1b1a",  # IcarusTrading 100% win, 85% politics/geo
]

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = "polymarket_bot.db"
