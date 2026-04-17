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
                       market_question: str = "",
                       cancel_order_id: str = "",
                       aggressive: bool = False) -> Optional[str]:
    """
    Submit a SELL order on the CLOB to exit a position.

    Fetches the current best-bid price so the order fills immediately rather
    than sitting as an unfilled GTC order above the market.

    Uses the actual token balance from the Polymarket positions API as the sell
    size — recalculating from size_usdc / entry_price causes rounding mismatches
    that the CLOB rejects with "not enough balance".

    token_id   : CLOB token ID for the outcome we hold
    shares     : fallback share count if the positions API lookup fails
    aggressive : if True, price 5% below best_bid to guarantee fill in fast markets
                 (used for stop-loss exits where getting out matters more than price)

    Returns order_id on success, "" in paper mode, None on failure.
    """
    import api_client as _api

    mode_label = "PAPER" if PAPER_TRADE else "LIVE"

    if PAPER_TRADE:
        logger.info("[PAPER] Sell | token=%s shares=%.4f  %s",
                    token_id[:12], shares, market_question[:50])
        return ""

    # Use actual token balance from Polymarket to avoid rounding mismatches
    if FUNDER_ADDRESS:
        actual_shares = _api.get_token_balance(FUNDER_ADDRESS, token_id)
        if actual_shares is not None and actual_shares > 0:
            if abs(actual_shares - shares) > 0.01:
                logger.info(
                    "execute_sell_trade: using actual balance %.4f (DB-calculated was %.4f)",
                    actual_shares, shares,
                )
            shares = actual_shares
        elif actual_shares == 0 or actual_shares is None:
            logger.warning(
                "execute_sell_trade: no token balance found on Polymarket for token %s — "
                "may already be sold or market resolved; skipping",
                token_id[:12],
            )
            return ""  # treat as already closed, don't block the DB close

    # Cancel any previous unfilled sell order before placing a fresh one.
    # This prevents accumulating multiple open sell orders for the same position.
    if cancel_order_id:
        client = _get_clob_client()
        if client:
            try:
                client.cancel(cancel_order_id)
                logger.info("execute_sell_trade: cancelled previous order %s before retry", cancel_order_id[:18])
            except Exception as exc:
                logger.debug("execute_sell_trade: cancel %s failed (may already be filled/gone): %s", cancel_order_id[:18], exc)

    # Get best bid — the price buyers are willing to pay right now
    best_bid = _api.get_best_bid(token_id)
    if best_bid is None:
        logger.error("execute_sell_trade: cannot get best bid for token %s", token_id[:12])
        return None

    # A best_bid at or above 0.999 means the market resolved in our favour.
    # The CLOB rejects sell orders priced at 1.0 (out-of-range), and Polymarket
    # pays out the winning shares automatically on-chain — no sell order needed.
    if best_bid >= 0.999:
        logger.info(
            "execute_sell_trade: best_bid=%.4f ≥ 0.999 — market appears resolved (win); "
            "skipping SELL, on-chain payout will follow | token=%s  %s",
            best_bid, token_id[:12], market_question[:50],
        )
        return ""  # empty string = success; caller will close the DB record

    # Aggressive mode: price 5% below best_bid to guarantee fill in fast markets.
    # The CLOB tick size is 0.001, so round down to nearest tick.
    sell_price = best_bid
    if aggressive:
        sell_price = max(0.001, round(best_bid * 0.95, 3))
        logger.info(
            "[%s] Selling AGGRESSIVE | token=%s best_bid=%.4f sell_price=%.4f (-5%%) shares=%.4f  %s",
            mode_label, token_id[:12], best_bid, sell_price, shares, market_question[:50],
        )
    else:
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
            price=sell_price,
            size=shares,
            side="SELL",
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id") or ""
        logger.info("Live SELL placed | order_id=%s token=%s price=%.4f shares=%.4f",
                    order_id, token_id[:12], sell_price, shares)
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
            if not resp or not isinstance(resp, dict):
                logger.debug("reconcile_open_orders: no data for order %s — skipping", order_id[:18])
                continue
            clob_status = resp.get("status", "")
            size_matched = float(resp.get("size_matched") or 0)

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
                import position_manager as _pm
                _pm.mark_market_cancelled(trade.get("market_id") or "")
            elif clob_status == "LIVE" and size_matched == 0:
                # Order is sitting on the book with zero fills — cancel it so it
                # doesn't occupy a position slot indefinitely.
                logger.warning(
                    "Order %s is LIVE but unfilled — cancelling (trade id=%d, market=%s)",
                    order_id[:18], trade["id"],
                    (trade.get("market_question") or "")[:40],
                )
                try:
                    client.cancel(order_id)
                except Exception:
                    pass
                database.cancel_trade(
                    trade["id"],
                    reason="CLOB order LIVE but never filled — cancelled to free position slot",
                )
                import position_manager as _pm
                _pm.mark_market_cancelled(trade.get("market_id") or "")
        except Exception as exc:
            logger.error("reconcile_open_orders: failed to check order %s: %s", order_id[:18], exc)


_redeemable_ids_seen: set = set()


def redeem_won_positions() -> int:
    """
    Scan for positions on Polymarket that are redeemable (market resolved WIN).

    Redemption requires the Polymarket proxy wallet's owner key, which the bot
    doesn't hold.  Instead, this function detects redeemable positions, logs them
    clearly, and sends a Telegram alert so the user can redeem manually at
    polymarket.com.

    Safe to call every cycle — no-ops in paper mode.  Suppresses repeated alerts
    by only notifying when the set of redeemable positions changes.
    Returns number of distinct redeemable positions found.
    """
    global _redeemable_ids_seen

    if PAPER_TRADE:
        return 0
    if not FUNDER_ADDRESS:
        return 0

    import api_client as _api
    import notifier

    positions = _api.get_wallet_positions(FUNDER_ADDRESS)
    if not positions:
        return 0

    # A position is redeemable when the market resolved in our favour.
    # The Data API returns a 'redeemable' boolean — fall back to detecting it via
    # currentValue ≈ size (shares each worth $1.00 = fully resolved WIN).
    redeemable = []
    for p in positions:
        size = float(p.get("size") or 0)
        if size <= 0:
            continue
        if p.get("redeemable"):
            redeemable.append(p)
            continue
        current_val = float(p.get("currentValue") or 0)
        if current_val >= size * 0.999:
            redeemable.append(p)

    if not redeemable:
        _redeemable_ids_seen = set()  # reset so next batch triggers alert
        return 0

    # Only alert when the set of redeemable positions changes
    current_ids = {(p.get("conditionId") or p.get("asset") or "")
                   for p in redeemable}
    last_ids    = _redeemable_ids_seen
    new_ids     = current_ids - last_ids
    _redeemable_ids_seen = current_ids

    total_usdc = round(sum(float(p.get("currentValue") or 0) for p in redeemable), 2)

    if new_ids:
        lines = []
        for p in redeemable:
            pid = p.get("conditionId") or p.get("asset") or ""
            if pid not in new_ids:
                continue
            title   = (p.get("title") or pid[:12])[:50]
            outcome = p.get("outcome") or ""
            val     = float(p.get("currentValue") or 0)
            lines.append(f"  • {title} / {outcome}  ${val:.2f}")
        summary = "\n".join(lines)
        logger.warning(
            "redeem_won_positions: %d position(s) need manual redemption "
            "(total ~$%.2f USDC) — go to polymarket.com to claim:\n%s",
            len(redeemable), total_usdc, summary,
        )
        notifier.notify_redemption_needed(redeemable, total_usdc)
    else:
        logger.info(
            "redeem_won_positions: %d pending redemption(s) unchanged (~$%.2f USDC) "
            "— redeem at polymarket.com",
            len(redeemable), total_usdc,
        )

    return len(redeemable)


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
