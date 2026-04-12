"""
Trade executor — submits orders to the Polymarket CLOB.

In PAPER_TRADE mode (default) it only logs what would have been submitted.
In live mode it uses py-clob-client to sign and submit a limit order.
"""

import logging
from typing import Optional

import database
from config import PAPER_TRADE, PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASS, CLOB_HOST
from wallet_monitor import WhaleTrade

logger = logging.getLogger(__name__)

# Lazy-initialised CLOB client (only needed in live mode)
_clob_client = None


def _get_clob_client():
    """Initialise the py-clob-client on first use."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=POLY_API_KEY,
            api_secret=POLY_API_SECRET,
            api_passphrase=POLY_API_PASS,
        )
        _clob_client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=137,   # Polygon mainnet
            creds=creds,
            signature_type=2,   # POLY_PROXY
        )
        logger.info("CLOB client initialised")
    except ImportError:
        logger.error("py-clob-client not installed. Run: pip install py-clob-client")
    except Exception as exc:
        logger.error("Failed to initialise CLOB client: %s", exc)
    return _clob_client


def _place_live_order(token_id: str, price: float, size_usdc: float) -> Optional[str]:
    """
    Submit a limit BUY order on the CLOB.
    Returns order_id on success, None on failure.
    """
    client = _get_clob_client()
    if not client:
        return None
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        # size in CLOB = number of shares = usdc / price
        shares = round(size_usdc / price, 4) if price > 0 else 0
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id") or ""
        logger.info("Live order placed | order_id=%s token=%s price=%.4f shares=%.4f",
                    order_id, token_id[:12], price, shares)
        return order_id
    except Exception as exc:
        logger.error("Order placement failed: %s", exc)
        return None


def execute_copy_trade(trade: WhaleTrade, size_usdc: float,
                       whale_trade_id: int) -> Optional[int]:
    """
    Execute (or paper-log) a copy of the given whale trade.

    Returns the copy_trade DB row id on success, None on failure.
    """
    mode_label = "PAPER" if PAPER_TRADE else "LIVE"

    logger.info(
        "[%s] Copying trade | market=%s outcome=%s price=%.4f size=%.2f USDC",
        mode_label,
        trade.market_question[:50],
        trade.outcome,
        trade.price,
        size_usdc,
    )

    order_id = ""

    if not PAPER_TRADE:
        order_id = _place_live_order(trade.token_id, trade.price, size_usdc) or ""
        if not order_id:
            logger.error("Live order failed — trade not recorded")
            return None

    copy_id = database.record_copy_trade(
        whale_trade_id=whale_trade_id,
        market_id=trade.market_id,
        market_question=trade.market_question,
        token_id=trade.token_id,
        outcome=trade.outcome,
        side="BUY",
        price=trade.price,
        size_usdc=size_usdc,
        order_id=order_id,
        paper_trade=PAPER_TRADE,
    )

    logger.info("[%s] Trade recorded | copy_trade_id=%d", mode_label, copy_id)
    return copy_id


def get_live_balance() -> float:
    """Returns current USDC balance from the CLOB account (live mode only)."""
    if PAPER_TRADE:
        return 0.0
    client = _get_clob_client()
    if not client:
        return 0.0
    try:
        return float(client.get_balance())
    except Exception as exc:
        logger.error("Failed to get balance: %s", exc)
        return 0.0
