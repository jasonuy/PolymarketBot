"""
Position manager — decides HOW MUCH to trade and WHETHER to trade.

Rules enforced:
  - Max open positions (MAX_OPEN_POSITIONS)
  - Max fraction of bankroll per trade (MAX_POSITION_FRACTION)
  - Absolute USDC cap per trade (MAX_TRADE_USDC)
  - Skip illiquid markets (MAX_SPREAD_PCT)
  - Never trade a market we already have an open position in
  - Trust-based size multiplier: high-trust whales get 2x, low-trust get 0.5x
  - Dead-zone avoidance: sizes in $30-40 snapped down to $30 (historically worst bucket)
"""

import logging
from wallet_monitor import WhaleTrade
import api_client
import database
from config import (
    MAX_POSITION_FRACTION,
    MAX_TRADE_USDC,
    MAX_OPEN_POSITIONS,
    MAX_SPREAD_PCT,
    PAPER_TRADE,
)

logger = logging.getLogger(__name__)


def _live_open_positions() -> list[dict]:
    """Returns only non-paper open positions — the ones that count against limits."""
    return [p for p in database.get_open_positions() if not p.get("paper_trade")]


def _open_position_count() -> int:
    return len(_live_open_positions())


def _already_in_market(market_id: str) -> bool:
    return any(p["market_id"] == market_id for p in _live_open_positions())


def _open_positions_for_wallet(wallet: str) -> int:
    return sum(1 for p in _live_open_positions() if p.get("source_wallet") == wallet)


def _check_liquidity(token_id: str, market_id: str = "", slug: str = "") -> bool:
    """Returns True if the market is liquid enough to enter.
    Skipped in paper trade mode — no real order is being placed.

    Spread lookup order:
      1. GAMMA by slug  — most reliable; handles neg-risk game markets where
         the individual CLOB token book always shows ~99% spread.
      2. GAMMA by conditionId — works for standard binary markets.
      3. CLOB token order book — last resort fallback.
    """
    if PAPER_TRADE:
        return True

    # Try GAMMA first (handles neg-risk markets correctly)
    spread = api_client.get_spread_from_gamma(market_id, slug=slug)

    # Fall back to raw CLOB token book
    if spread is None:
        spread = api_client.get_spread(token_id)

    if spread is None:
        logger.warning("Could not fetch spread for token %s — skipping", token_id)
        return False
    if spread > MAX_SPREAD_PCT:
        logger.info("Spread %.1f%% exceeds max %.1f%% — skipping", spread * 100, MAX_SPREAD_PCT * 100)
        return False
    return True


def should_copy(trade: WhaleTrade, bankroll_usdc: float) -> tuple[bool, float]:
    """
    Decide whether to copy a whale trade and how much USDC to size it at.

    Returns:
        (should_trade: bool, size_usdc: float)
    """
    # 1. Cap on open positions
    open_count = _open_position_count()
    if open_count >= MAX_OPEN_POSITIONS:
        logger.info("Max open positions (%d) reached — skipping", MAX_OPEN_POSITIONS)
        return False, 0.0

    # 2. Per-wallet trust cap — trust level is earned dynamically (win=+1, loss=-1)
    trust_level = database.get_wallet_trust_level(trade.wallet)
    wallet_open = _open_positions_for_wallet(trade.wallet)
    if wallet_open >= trust_level:
        logger.info(
            "Wallet %s... already has %d open position(s) — skipping (trust level: %d)",
            trade.wallet[:10], wallet_open, trust_level,
        )
        return False, 0.0

    # 3. Don't double up on the same market
    if _already_in_market(trade.market_id):
        logger.info("Already have an open position in market %s — skipping", trade.market_id[:20])
        return False, 0.0

    # 4. Liquidity check
    if not _check_liquidity(trade.token_id, market_id=trade.market_id, slug=trade.slug):
        return False, 0.0

    # 5. Calculate base position size
    fractional_size = bankroll_usdc * MAX_POSITION_FRACTION
    size_usdc = min(fractional_size, MAX_TRADE_USDC)

    if size_usdc < 1.0:
        logger.info("Calculated size %.4f USDC is too small — skipping", size_usdc)
        return False, 0.0

    # 6. Trust-based size multiplier
    #    High-trust whales (5+) have demonstrated good returns in the $40-50 bucket →
    #    double their size to target that range.
    #    Low-trust whales (1-2) have poor track records → halve their size to limit
    #    exposure while still allowing them to trade.
    if trust_level >= 5:
        multiplier = 2.0
    elif trust_level <= 2:
        multiplier = 0.5
    else:
        multiplier = 1.0

    size_usdc = min(size_usdc * multiplier, MAX_TRADE_USDC * multiplier)

    # 7. Dead-zone avoidance: $30-40 is historically the worst bucket (2W/7L).
    #    Snap any size that lands there down to $30 to stay in the profitable $20-30 range.
    if 30.0 < size_usdc < 40.0:
        logger.info(
            "Dead-zone avoidance: size %.2f snapped to $30.00 (avoiding $30-40 bucket)",
            size_usdc,
        )
        size_usdc = 30.0

    logger.info(
        "Position approved | size=%.2f USDC (bankroll=%.2f, fraction=%.0f%%, cap=%.0f, trust=%d, mult=%.1fx)",
        size_usdc, bankroll_usdc, MAX_POSITION_FRACTION * 100, MAX_TRADE_USDC, trust_level, multiplier,
    )
    return True, round(size_usdc, 2)


def calculate_pnl(entry_price: float, exit_price: float,
                  size_usdc: float, side: str) -> float:
    """
    Simple P&L calculation.
    On Polymarket, YES tokens resolve to $1 (win) or $0 (loss).
    P&L = (exit_price - entry_price) * shares
    shares = size_usdc / entry_price
    """
    if entry_price <= 0:
        return 0.0
    shares = size_usdc / entry_price
    pnl = (exit_price - entry_price) * shares
    return round(pnl, 4)
