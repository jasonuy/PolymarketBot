"""
Polymarket Copy-Trade Bot
─────────────────────────
Main entry point. Run with:

    python bot.py

The bot will:
  1. Poll watched wallets every POLL_INTERVAL seconds
  2. Detect new trades made by those wallets
  3. Apply risk management rules (position sizing, spread check, etc.)
  4. Copy qualifying trades (paper or live depending on PAPER_TRADE setting)
  5. Send Telegram alerts for every action
  6. Log everything to SQLite

Start in PAPER_TRADE=true mode (the default) until you've validated the
wallet list and logic. Then switch to live by setting PAPER_TRADE=false in .env
"""

import logging
import time
import colorlog
from datetime import datetime, UTC

import database
import notifier
import api_client
import wallet_monitor
import position_manager
import trade_executor
import wallet_discovery
from config import (
    PAPER_TRADE,
    POLL_INTERVAL,
    WATCHED_WALLETS,
    MAX_TRADE_USDC,
    LIVE_BANKROLL,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    MIN_TRADES_BEFORE_FILTER,
    MIN_WIN_RATE_TO_FOLLOW,
    FUNDER_ADDRESS,
)

# Runtime wallet list — starts from config, topped up by auto-discovery
_active_wallets: list[str] = []


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> None:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    # Also write to a file
    file_handler = logging.FileHandler("bot.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)


logger = logging.getLogger(__name__)


# ── Core loop ─────────────────────────────────────────────────────────────────

def import_trade_history() -> None:
    """
    Download full wallet trade history from the Polymarket Data API and
    reconcile it with the DB.

    - Closed positions (have both BUYs and SELLs for the same token):
        • If already in DB → update to CLOSED with actual P&L
        • If missing from DB → insert as a CLOSED live trade
    - Open positions (BUY only) → already handled by sync_positions_with_polymarket()
    """
    if not FUNDER_ADDRESS:
        logger.warning("import_trade_history: FUNDER_ADDRESS not set — skipping")
        return

    logger.info("Downloading trade history from Polymarket...")

    # Paginate through all activity
    all_trades: list[dict] = []
    limit, offset = 100, 0
    while True:
        page = api_client.get_wallet_activity(FUNDER_ADDRESS, limit=limit)
        # get_wallet_activity doesn't support offset, so fall back to direct call
        import requests as _req
        from config import DATA_API
        resp = _req.get(f"{DATA_API}/activity",
                        params={"user": FUNDER_ADDRESS, "limit": limit, "offset": offset},
                        timeout=10)
        if not resp.ok:
            break
        page = resp.json()
        if not isinstance(page, list) or not page:
            break
        all_trades.extend(page)
        if len(page) < limit:
            break
        offset += limit

    logger.info("import_trade_history: fetched %d trades", len(all_trades))

    # Group by asset (token_id) — uniquely identifies one outcome of one market
    from collections import defaultdict
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for t in all_trades:
        by_asset[t["asset"]].append(t)

    # Fetch existing DB records for quick lookup
    db_open   = database.get_open_positions()
    db_open_by_token = {(t.get("token_id") or "").lower(): t for t in db_open if t.get("token_id")}
    db_open_by_key   = {
        ((t.get("market_id") or "").lower(), (t.get("outcome") or "").lower()): t
        for t in db_open
    }

    inserted_count = 0
    closed_count   = 0

    for asset, txs in by_asset.items():
        buys  = [t for t in txs if t["side"] == "BUY"]
        sells = [t for t in txs if t["side"] == "SELL"]

        if not sells:
            continue  # Still open — handled by startup sync

        total_buy_usdc  = sum(t["usdcSize"] for t in buys)
        total_sell_usdc = sum(t["usdcSize"] for t in sells)
        pnl             = round(total_sell_usdc - total_buy_usdc, 4)
        avg_buy_price   = round(
            sum(t["price"] * t["usdcSize"] for t in buys) / total_buy_usdc, 6
        ) if total_buy_usdc else 0.0
        # Weighted average sell price
        avg_sell_price = round(
            sum(t["price"] * t["usdcSize"] for t in sells) / total_sell_usdc, 6
        ) if total_sell_usdc else 0.0
        # Most recent sell timestamp (Unix → ISO)
        last_sell_ts = max(t["timestamp"] for t in sells)
        from datetime import timezone
        closed_at = datetime.fromtimestamp(last_sell_ts, tz=timezone.utc).isoformat()

        sample    = txs[0]
        cid       = sample.get("conditionId") or ""
        outcome   = sample.get("outcome") or ""
        title     = sample.get("title") or ""
        close_reason = "WIN" if pnl > 0 else "LOSS"

        # Check if this position already exists in the DB (open or closed)
        db_trade = (
            db_open_by_token.get(asset.lower()) or
            db_open_by_key.get((cid.lower(), outcome.lower()))
        )

        if db_trade:
            # Close the existing open record with real P&L
            logger.info(
                "import_trade_history: closing DB id=%d  %s / %s  pnl=%+.2f",
                db_trade["id"], title[:40], outcome, pnl,
            )
            database.close_trade(
                db_trade["id"],
                close_price=avg_sell_price,
                pnl_usdc=pnl,
                close_reason=f"Market resolved — {close_reason}",
            )
            closed_count += 1
        else:
            # Check if it was already imported as a closed record to avoid duplicates
            with database.get_conn() as conn:
                existing = conn.execute(
                    """SELECT id FROM copy_trades
                       WHERE token_id=? AND status='CLOSED'
                       LIMIT 1""",
                    (asset,)
                ).fetchone()
            if existing:
                logger.debug(
                    "import_trade_history: already in DB (id=%d) — skipping %s / %s",
                    existing["id"], title[:40], outcome,
                )
                continue

            # Insert as a new closed live trade
            logger.info(
                "import_trade_history: inserting closed  %s / %s  pnl=%+.2f",
                title[:40], outcome, pnl,
            )
            new_id = database.record_copy_trade(
                whale_trade_id=None,
                market_id=cid,
                market_question=title,
                token_id=asset,
                outcome=outcome,
                side="BUY",
                price=avg_buy_price,
                size_usdc=total_buy_usdc,
                order_id="",
                paper_trade=False,
            )
            database.close_trade(
                new_id,
                close_price=avg_sell_price,
                pnl_usdc=pnl,
                close_reason=f"Market resolved — {close_reason}",
            )
            # Patch closed_at to the real timestamp (not now)
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE copy_trades SET closed_at=? WHERE id=?",
                    (closed_at, new_id)
                )
            inserted_count += 1

    total_pnl = sum(
        round(sum(t["usdcSize"] for t in txs if t["side"] == "SELL") -
              sum(t["usdcSize"] for t in txs if t["side"] == "BUY"), 2)
        for txs in by_asset.values()
        if any(t["side"] == "SELL" for t in txs)
    )
    logger.info(
        "import_trade_history: done — %d inserted, %d updated, total closed P&L = %+.2f USDC",
        inserted_count, closed_count, total_pnl,
    )


def sync_positions_with_polymarket() -> None:
    """
    Sync the DB with live Polymarket positions on startup.
    Polymarket is the gold standard.

      - Positions on Polymarket not in DB  → inserted as live open trades
      - Live DB positions not on Polymarket → closed (resolved or sold externally)
      - Paper trades in DB                 → untouched (never placed on Polymarket)
    """
    if not FUNDER_ADDRESS:
        logger.warning("startup_sync: FUNDER_ADDRESS not set — skipping sync")
        return

    logger.info("Syncing positions with Polymarket...")

    poly_positions = api_client.get_wallet_positions(FUNDER_ADDRESS)
    if poly_positions is None:
        logger.error("startup_sync: failed to fetch Polymarket positions — skipping")
        return

    # Build lookup keyed by (conditionId.lower(), outcome.lower())
    poly_by_key: dict[tuple, dict] = {}
    for p in poly_positions:
        cid     = (p.get("conditionId") or "").lower()
        outcome = (p.get("outcome") or "").lower()
        if cid:
            poly_by_key[(cid, outcome)] = p

    db_open = database.get_open_positions()
    db_live_matched: set[tuple] = set()
    closed_count = 0

    # Check each live (non-paper) DB position against Polymarket
    for trade in db_open:
        if trade.get("paper_trade"):
            continue

        cid     = (trade.get("market_id") or "").lower()
        outcome = (trade.get("outcome") or "").lower()
        key     = (cid, outcome)

        if key in poly_by_key:
            db_live_matched.add(key)
            logger.info(
                "startup_sync: verified   id=%-3d  %s / %s",
                trade["id"],
                (trade.get("market_question") or cid)[:45],
                trade.get("outcome"),
            )
        else:
            # Before closing, check if the CLOB order is still active (filling or live).
            # If so, skip — the position just hasn't propagated to the Data API yet.
            order_id = trade.get("order_id") or ""
            if order_id and not PAPER_TRADE:
                try:
                    clob_client = trade_executor._get_clob_client()
                    if clob_client:
                        resp = clob_client.get_order(order_id)
                        clob_status = resp.get("status", "")
                        if clob_status in ("LIVE", "MATCHED"):
                            logger.info(
                                "startup_sync: order %s is %s on CLOB — skipping close for id=%d",
                                order_id[:18], clob_status, trade["id"],
                            )
                            db_live_matched.add(key)  # treat as matched
                            continue
                except Exception:
                    pass

            logger.warning(
                "startup_sync: not on Polymarket — closing  id=%d  %s / %s",
                trade["id"],
                (trade.get("market_question") or cid)[:45],
                trade.get("outcome"),
            )
            database.close_trade(
                trade["id"],
                close_price=0.0,
                pnl_usdc=-trade["size_usdc"],
                close_reason="Not found on Polymarket at startup sync (resolved or sold externally)",
            )
            closed_count += 1

    # Insert Polymarket positions that have no matching DB record
    # Build set of token_ids already closed in DB to avoid re-inserting zero-value resolved positions
    with database.get_conn() as conn:
        closed_tokens: set[str] = {
            row[0] for row in conn.execute(
                "SELECT token_id FROM copy_trades WHERE status IN ('CLOSED','CANCELLED') AND token_id IS NOT NULL"
            ).fetchall()
        }

    inserted_count = 0
    for key, p in poly_by_key.items():
        if key in db_live_matched:
            continue
        title       = p.get("title") or ""
        outcome     = p.get("outcome") or ""
        avg_price   = float(p.get("avgPrice") or 0)
        initial_val = float(p.get("initialValue") or 0)
        asset       = p.get("asset") or ""
        cid         = p.get("conditionId") or ""

        # Skip positions already closed in DB (e.g. resolved at $0, not yet redeemed on-chain)
        if asset in closed_tokens:
            logger.debug("startup_sync: skipping %s / %s — already closed in DB", title[:40], outcome)
            continue
        logger.info(
            "startup_sync: inserting  %s / %s  (avg_price=%.4f  size=%.2f USDC)",
            title[:45], outcome, avg_price, initial_val,
        )
        database.record_copy_trade(
            whale_trade_id=None,
            market_id=cid,
            market_question=title,
            token_id=asset,
            outcome=outcome,
            side="BUY",
            price=avg_price,
            size_usdc=initial_val,
            order_id="",
            paper_trade=False,
        )
        inserted_count += 1

    logger.info(
        "startup_sync: done — %d on Polymarket | %d inserted | %d closed as external",
        len(poly_positions), inserted_count, closed_count,
    )


def process_whale_trade(trade: wallet_monitor.WhaleTrade) -> None:
    """Handle a single newly detected whale trade."""

    # ── Wallet performance filter ─────────────────────────────────────────────
    stats = database.get_wallet_stats(trade.wallet)
    if stats and stats["total_copies"] >= MIN_TRADES_BEFORE_FILTER:
        win_rate = stats["wins"] / stats["total_copies"]
        if win_rate < MIN_WIN_RATE_TO_FOLLOW:
            logger.warning(
                "Wallet %s...  muted — win rate %.0f%% (%d W / %d L, P&L %+.2f) below %.0f%% threshold",
                trade.wallet[:10], win_rate * 100,
                stats["wins"], stats["losses"], stats["total_pnl"],
                MIN_WIN_RATE_TO_FOLLOW * 100,
            )
            notifier.notify_skipped(
                trade.market_question,
                f"Wallet {trade.wallet[:10]}... muted — "
                f"{win_rate:.0%} win rate on {stats['total_copies']} trades "
                f"(P&L {stats['total_pnl']:+.2f} USDC)",
            )
            return

    # Record the whale trade regardless of whether we copy it
    whale_id = database.record_whale_trade(
        wallet=trade.wallet,
        market_id=trade.market_id,
        market_question=trade.market_question,
        outcome=trade.outcome,
        side=trade.side,
        price=trade.price,
        size_usdc=trade.size_usdc,
        tx_hash=trade.tx_hash,
    )

    notifier.notify_whale_detected(
        wallet=trade.wallet,
        question=trade.market_question,
        outcome=trade.outcome,
        price=trade.price,
        size_usdc=trade.size_usdc,
    )

    # Determine bankroll: in paper mode we use MAX_TRADE_USDC * 20 as a
    # synthetic bankroll so sizing math still works meaningfully
    if PAPER_TRADE:
        bankroll = MAX_TRADE_USDC * 20
    elif LIVE_BANKROLL > 0:
        bankroll = LIVE_BANKROLL
    else:
        bankroll = trade_executor.get_live_balance()
        if bankroll <= 0:
            logger.error("Cannot determine live bankroll — skipping trade")
            notifier.notify_error("Cannot determine live bankroll — trade skipped")
            return

    should_trade, size_usdc = position_manager.should_copy(trade, bankroll)

    if not should_trade:
        notifier.notify_skipped(trade.market_question, "Risk rules blocked this trade")
        return

    copy_id = trade_executor.execute_copy_trade(trade, size_usdc, whale_id)

    if copy_id is not None:
        notifier.notify_trade_placed(
            question=trade.market_question,
            outcome=trade.outcome,
            price=trade.price,
            size_usdc=size_usdc,
            paper=PAPER_TRADE,
        )


def check_open_positions() -> None:
    """
    Check every open position for:
      0. Order reconciliation — cancel any DB records whose CLOB order is INVALID/CANCELLED.
      1. Market resolution — GAMMA API reports the market as closed with a final price.
      2. Take-profit      — current CLOB price rose >= TAKE_PROFIT_PCT from entry.
      3. Stop-loss        — current CLOB price dropped >= STOP_LOSS_PCT from entry.
    Closes any position that meets a criterion and records P&L.
    """
    trade_executor.reconcile_open_orders()
    trade_executor.redeem_won_positions()
    positions = database.get_open_positions()
    if not positions:
        return

    logger.info("Checking %d open position(s) for exit signals...", len(positions))

    for pos in positions:
        pos_id          = pos["id"]
        entry_price     = pos["price"]
        size_usdc       = pos["size_usdc"]
        outcome         = pos["outcome"]
        market_id       = pos["market_id"]
        token_id        = pos.get("token_id") or ""
        market_question = pos.get("market_question") or market_id

        close_price = None
        close_reason = None

        # 1. Live price check via CLOB — enables stop-loss / take-profit
        if token_id and entry_price > 0:
            current_price = api_client.get_market_price(token_id)
            if current_price is not None:
                change_pct = (current_price - entry_price) / entry_price
                if change_pct <= -STOP_LOSS_PCT:
                    close_price  = current_price
                    close_reason = f"Stop-loss ({change_pct:+.1%} from entry)"
                elif change_pct >= TAKE_PROFIT_PCT:
                    close_price  = current_price
                    close_reason = f"Take-profit ({change_pct:+.1%} from entry)"
                elif current_price >= 0.99:
                    # Price converged to ~$1.00 — market has effectively resolved as a WIN.
                    # The CLOB may not mark it as closed=True for hours, but the price
                    # already reflects certainty. Close now to free up the position slot.
                    close_price  = current_price
                    close_reason = f"Near-resolved WIN ({current_price:.4f})"

        # 2. Market resolution check via GAMMA API
        if close_price is None and market_id:
            resolution = api_client.get_market_resolution(market_id, outcome)
            if resolution is not None:
                close_price  = resolution
                result       = "WIN" if resolution >= 0.5 else "LOSS"
                close_reason = f"Market resolved — {result}"

        if close_price is not None:
            paper = pos.get("paper_trade")

            # For stop-loss / take-profit: place a real SELL order on the CLOB.
            # For resolved markets: no sell needed — Polymarket pays out automatically on-chain.
            # Skip sell for paper trades entirely.
            is_resolution = close_reason and "resolved" in close_reason.lower()
            if not paper and token_id and not is_resolution:
                shares = round(size_usdc / entry_price, 4) if entry_price > 0 else 0
                sell_id = trade_executor.execute_sell_trade(
                    token_id=token_id,
                    shares=shares,
                    market_question=market_question,
                    cancel_order_id=pos.get("order_id") or "",
                )
                if sell_id is None:
                    # Live sell failed — don't close the DB record, try again next cycle
                    logger.error(
                        "Sell order failed — keeping position open in DB (id=%d) to retry",
                        pos_id,
                    )
                    continue

            pnl = position_manager.calculate_pnl(entry_price, close_price, size_usdc, "BUY")
            database.close_trade(pos_id, close_price, pnl, close_reason or "")

            source_wallet = pos.get("source_wallet") or ""
            if source_wallet:
                database.update_wallet_stats(source_wallet, pnl)
                stats = database.get_wallet_stats(source_wallet)
                if stats and stats["total_copies"] >= MIN_TRADES_BEFORE_FILTER:
                    win_rate = stats["wins"] / stats["total_copies"]
                    if win_rate < MIN_WIN_RATE_TO_FOLLOW:
                        logger.warning(
                            "Wallet %s... now below %.0f%% win rate threshold "
                            "(%.0f%% — %d W / %d L). Future signals will be skipped.",
                            source_wallet[:10], MIN_WIN_RATE_TO_FOLLOW * 100,
                            win_rate * 100, stats["wins"], stats["losses"],
                        )

            logger.info(
                "Position closed | market=%s outcome=%s entry=%.3f exit=%.3f pnl=%+.2f | %s",
                market_question[:40], outcome, entry_price, close_price, pnl, close_reason,
            )
            notifier.notify_trade_closed(
                question=market_question,
                outcome=outcome,
                entry_price=entry_price,
                close_price=close_price,
                size_usdc=size_usdc,
                pnl=pnl,
                reason=close_reason,
            )


def run_once() -> None:
    """Single scan cycle — called every POLL_INTERVAL seconds."""
    logger.info("-- Scanning wallets (%s) --", datetime.now(UTC).strftime("%H:%M:%S UTC"))
    check_open_positions()
    for trade in wallet_monitor.scan_wallets():
        try:
            process_whale_trade(trade)
        except Exception as exc:
            logger.exception("Error processing whale trade: %s", exc)
            notifier.notify_error(str(exc))


def print_status() -> None:
    """Print a P&L summary to the console."""
    summary = database.get_pnl_summary()
    logger.info(
        "P&L Summary | total=%d open=%d closed=%d pnl=%+.2f USDC",
        summary.get("total_trades", 0) or 0,
        summary.get("open", 0) or 0,
        summary.get("closed", 0) or 0,
        summary.get("total_pnl", 0.0) or 0.0,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def _init_wallets() -> list[str]:
    """
    Build the runtime wallet list.
    Uses WATCHED_WALLETS from config if populated, otherwise auto-discovers
    the top wallets from the Polymarket leaderboard.
    """
    global _active_wallets

    if WATCHED_WALLETS:
        logger.info("Using %d manually configured wallet(s)", len(WATCHED_WALLETS))
        _active_wallets = list(WATCHED_WALLETS)
    else:
        logger.info("No wallets configured — auto-discovering top wallets from leaderboard...")
        _active_wallets = wallet_discovery.discover_top_wallets()

        if not _active_wallets:
            logger.error(
                "Auto-discovery returned no wallets. "
                "Check your internet connection or add wallets manually in config.py"
            )

    # Inject into wallet_monitor so it uses our runtime list
    wallet_monitor.ACTIVE_WALLETS = _active_wallets
    return _active_wallets


def main() -> None:
    setup_logging()
    database.init_db()

    wallets = _init_wallets()

    sync_positions_with_polymarket()
    import_trade_history()

    mode = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    logger.info("=" * 60)
    logger.info("  Polymarket Copy-Trade Bot  |  Mode: %s", mode)
    logger.info("  Watching %d wallet(s)  |  Poll every %ds", len(wallets), POLL_INTERVAL)
    logger.info("=" * 60)

    notifier.notify_startup(paper_mode=PAPER_TRADE, wallet_count=len(wallets))

    cycle = 0
    _last_snapshot_date = ""

    while True:
        try:
            run_once()
            cycle += 1

            # Daily stats snapshot — once per UTC calendar day
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            if today != _last_snapshot_date:
                database.snapshot_daily_stats()
                _last_snapshot_date = today

            # Print P&L summary every 10 cycles
            if cycle % 10 == 0:
                print_status()
                notifier.notify_pnl_summary(database.get_pnl_summary())

        except KeyboardInterrupt:
            logger.info("Shutting down — goodbye")
            print_status()
            break
        except Exception as exc:
            logger.exception("Unexpected error in main loop: %s", exc)
            notifier.notify_error(f"Main loop error: {exc}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
