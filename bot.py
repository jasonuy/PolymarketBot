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
import wallet_monitor
import position_manager
import trade_executor
import wallet_discovery
from config import (
    PAPER_TRADE,
    POLL_INTERVAL,
    WATCHED_WALLETS,
    MAX_TRADE_USDC,
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

def process_whale_trade(trade: wallet_monitor.WhaleTrade) -> None:
    """Handle a single newly detected whale trade."""

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


def run_once() -> None:
    """Single scan cycle — called every POLL_INTERVAL seconds."""
    logger.info("── Scanning wallets (%s) ──", datetime.now(UTC).strftime("%H:%M:%S UTC"))
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

    mode = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    logger.info("=" * 60)
    logger.info("  Polymarket Copy-Trade Bot  |  Mode: %s", mode)
    logger.info("  Watching %d wallet(s)  |  Poll every %ds", len(wallets), POLL_INTERVAL)
    logger.info("=" * 60)

    notifier.notify_startup(paper_mode=PAPER_TRADE, wallet_count=len(wallets))

    cycle = 0
    while True:
        try:
            run_once()
            cycle += 1

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
