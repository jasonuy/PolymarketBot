"""
Wallet monitor — polls watched wallets and surfaces new trades.

For each watched wallet we:
  1. Pull the last N activity records from the Polymarket Data API.
  2. Skip anything we've already seen (deduped by tx_hash).
  3. Filter for BUY trades above MIN_WHALE_TRADE_USDC.
  4. Yield WhaleTrade dataclass instances for the bot to act on.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Iterator

import api_client
import database
from config import WATCHED_WALLETS, MIN_WHALE_TRADE_USDC

# Runtime wallet list — overridden by bot.py at startup with auto-discovered wallets
ACTIVE_WALLETS: list[str] = list(WATCHED_WALLETS)

logger = logging.getLogger(__name__)


@dataclass
class WhaleTrade:
    wallet:          str
    market_id:       str
    market_question: str
    outcome:         str        # e.g. "Yes" / "No"
    token_id:        str        # CLOB token id for this outcome
    side:            str        # "BUY" or "SELL"
    price:           float      # fill price (0–1)
    size_usdc:       float      # notional in USDC
    tx_hash:         str
    detected_at:     str


def _parse_activity(raw: dict, wallet: str) -> WhaleTrade | None:
    """
    Map a raw activity record from the Data API to a WhaleTrade.
    The API schema can change; this parser is deliberately defensive.
    """
    try:
        tx_hash  = raw.get("transactionHash") or raw.get("id") or ""
        side     = (raw.get("side") or raw.get("type") or "").upper()
        outcome  = raw.get("outcomeIndex") or raw.get("outcome") or ""
        price    = float(raw.get("price") or raw.get("usdcSize", 0) or 0)
        size     = float(raw.get("size") or raw.get("usdcSize") or 0)
        token_id = raw.get("assetId") or raw.get("tokenId") or ""

        # Try to infer USDC size — some responses give shares, others USDC
        usdc_size = float(raw.get("usdcSize") or (price * size) or 0)

        market_id       = raw.get("market") or raw.get("conditionId") or ""
        market_question = raw.get("title") or raw.get("question") or market_id

        if not all([tx_hash, side, market_id, token_id]):
            return None

        return WhaleTrade(
            wallet=wallet,
            market_id=market_id,
            market_question=market_question,
            outcome=str(outcome),
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=usdc_size,
            tx_hash=tx_hash,
            detected_at=datetime.now(UTC).isoformat(),
        )
    except (TypeError, ValueError, KeyError) as exc:
        logger.debug("Failed to parse activity record: %s — %s", raw, exc)
        return None


def scan_wallets() -> Iterator[WhaleTrade]:
    """
    Iterate over all watched wallets and yield new whale trades.
    Marks each tx as seen so it won't be yielded again.
    """
    if not ACTIVE_WALLETS:
        logger.warning("No wallets to monitor. Auto-discovery may have failed.")
        return

    for wallet in ACTIVE_WALLETS:
        logger.debug("Scanning wallet %s", wallet)
        activity = api_client.get_wallet_activity(wallet, limit=20)

        for raw in activity:
            trade = _parse_activity(raw, wallet)
            if trade is None:
                continue

            # Skip already-seen transactions
            if trade.tx_hash and database.is_tx_seen(trade.tx_hash):
                continue

            # Only follow BUY trades (entering a position)
            if trade.side != "BUY":
                database.mark_tx_seen(trade.tx_hash)
                continue

            # Filter out small trades — not worth copying
            if trade.size_usdc < MIN_WHALE_TRADE_USDC:
                database.mark_tx_seen(trade.tx_hash)
                continue

            # Mark as seen before yielding so a crash doesn't re-process it
            if trade.tx_hash:
                database.mark_tx_seen(trade.tx_hash)

            logger.info(
                "New whale trade | wallet=%s market=%s outcome=%s price=%.3f usdc=%.2f",
                wallet[:10] + "...", trade.market_question[:40],
                trade.outcome, trade.price, trade.size_usdc
            )
            yield trade
