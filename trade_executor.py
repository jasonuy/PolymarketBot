"""
Trade executor — submits orders to the Polymarket CLOB.

In PAPER_TRADE mode (default) it only logs what would have been submitted.
In live mode it uses py-clob-client to sign and submit a limit order.
"""

import logging
from typing import Optional

import database
from config import PAPER_TRADE, PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASS, CLOB_HOST, FUNDER_ADDRESS
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
            signature_type=2,   # Funder + Relayer (EOA signs, proxy wallet holds funds)
            funder=FUNDER_ADDRESS,
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


def execute_sell_trade(token_id: str, shares: float,
                       market_question: str = "") -> Optional[str]:
    """
    Submit a SELL order on the CLOB to exit a position.

    Fetches the current best-bid price so the order fills immediately rather
    than sitting as an unfilled GTC order above the market.

    token_id : CLOB token ID for the outcome we hold
    shares   : number of shares to sell (size_usdc / entry_price at buy time)

    Returns order_id on success, "" in paper mode, None on failure.
    """
    import api_client as _api

    mode_label = "PAPER" if PAPER_TRADE else "LIVE"

    if PAPER_TRADE:
        logger.info("[PAPER] Sell | token=%s shares=%.4f  %s",
                    token_id[:12], shares, market_question[:50])
        return ""

    # Get best bid — the price buyers are willing to pay right now
    best_bid = _api.get_best_bid(token_id)
    if best_bid is None:
        logger.error("execute_sell_trade: cannot get best bid for token %s", token_id[:12])
        return None

    logger.info(
        "[%s] Selling | token=%s best_bid=%.4f shares=%.4f  %s",
        mode_label, token_id[:12], best_bid, shares, market_question[:50],
    )

    client = _get_clob_client()
    if not client:
        return None
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        order_args = OrderArgs(
            token_id=token_id,
            price=best_bid,
            size=shares,
            side="SELL",
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id") or ""
        logger.info("Live SELL placed | order_id=%s token=%s bid=%.4f shares=%.4f",
                    order_id, token_id[:12], best_bid, shares)
        return order_id
    except Exception as exc:
        logger.error("Sell order failed: %s", exc)
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


def reconcile_open_orders() -> None:
    """
    Cross-check every open live order against the CLOB and cancel any that
    are INVALID or CANCELLED on-chain (i.e. were never actually filled).
    Safe to call every poll cycle — skips paper trades and no-ops if no client.
    """
    if PAPER_TRADE:
        return
    client = _get_clob_client()
    if not client:
        return

    open_trades = database.get_open_positions()
    live_open = [t for t in open_trades if not t.get("paper_trade") and t.get("order_id")]
    if not live_open:
        return

    for trade in live_open:
        order_id = trade["order_id"]
        try:
            resp = client.get_order(order_id)
            clob_status = resp.get("status", "")
            if clob_status in ("INVALID", "CANCELLED"):
                logger.warning(
                    "Order %s is %s on CLOB — cancelling DB record (trade id=%d, market=%s)",
                    order_id[:18], clob_status, trade["id"],
                    (trade.get("market_question") or "")[:40],
                )
                database.cancel_trade(
                    trade["id"],
                    reason=f"CLOB order {clob_status} — never filled",
                )
        except Exception as exc:
            logger.error("reconcile_open_orders: failed to check order %s: %s", order_id[:18], exc)


def get_live_balance() -> float:
    """Returns current USDC balance from the CLOB account."""
    client = _get_clob_client()
    if not client:
        return 0.0
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        result = client.get_balance_allowance(params)
        # CLOB returns USDC.e balance in raw units (6 decimals) — convert to dollars
        return round(float(result.get("balance", 0)) / 1_000_000, 2)
    except Exception as exc:
        logger.error("Failed to get balance: %s", exc)
        return 0.0
