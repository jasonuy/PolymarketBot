"""
Telegram notifier — sends alerts to your phone when the bot acts.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Message @userinfobot → copy your chat id
  3. Add both to your .env file
"""

import logging
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def _send(text: str) -> None:
    if not _ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def notify_whale_detected(wallet: str, question: str, outcome: str,
                          price: float, size_usdc: float) -> None:
    _send(
        f"🐳 <b>Whale detected</b>\n"
        f"Wallet: <code>{wallet[:10]}...</code>\n"
        f"Market: {question[:60]}\n"
        f"Outcome: {outcome} @ {price:.3f}\n"
        f"Size: ${size_usdc:,.2f} USDC"
    )


def notify_trade_placed(question: str, outcome: str, price: float,
                        size_usdc: float, paper: bool) -> None:
    mode = "📄 PAPER" if paper else "✅ LIVE"
    _send(
        f"{mode} <b>Trade copied</b>\n"
        f"Market: {question[:60]}\n"
        f"Outcome: {outcome} @ {price:.3f}\n"
        f"Size: ${size_usdc:,.2f} USDC"
    )


def notify_skipped(question: str, reason: str) -> None:
    _send(
        f"⏭ <b>Trade skipped</b>\n"
        f"Market: {question[:60]}\n"
        f"Reason: {reason}"
    )


def notify_error(message: str) -> None:
    _send(f"❌ <b>Bot error</b>\n{message}")


def notify_startup(paper_mode: bool, wallet_count: int) -> None:
    mode = "PAPER TRADE" if paper_mode else "LIVE TRADE"
    _send(
        f"🤖 <b>Polymarket copy bot started</b>\n"
        f"Mode: {mode}\n"
        f"Watching: {wallet_count} wallet(s)"
    )


def notify_trade_closed(question: str, outcome: str, entry_price: float,
                        close_price: float, size_usdc: float,
                        pnl: float, reason: str) -> None:
    emoji = "✅" if pnl >= 0 else "🔴"
    _send(
        f"{emoji} <b>Position closed</b>\n"
        f"Market: {question[:60]}\n"
        f"Outcome: {outcome}\n"
        f"Entry: {entry_price:.3f}  →  Exit: {close_price:.3f}\n"
        f"Size: ${size_usdc:,.2f}  |  P&L: {pnl:+.2f} USDC\n"
        f"Reason: {reason}"
    )


def notify_redemption_needed(positions: list, total_usdc: float) -> None:
    lines = []
    for p in positions[:10]:  # cap at 10 to avoid very long messages
        title   = (p.get("title") or "")[:45]
        outcome = p.get("outcome") or ""
        val     = float(p.get("currentValue") or 0)
        lines.append(f"  • {title} / {outcome}  ${val:.2f}")
    body = "\n".join(lines)
    if len(positions) > 10:
        body += f"\n  … and {len(positions) - 10} more"
    _send(
        f"💰 <b>Redemption needed</b>  (~${total_usdc:.2f} USDC)\n"
        f"Go to polymarket.com → Portfolio → Redeem:\n{body}"
    )


def notify_pnl_summary(summary: dict) -> None:
    _send(
        f"📊 <b>P&L Summary</b>\n"
        f"Total trades: {summary.get('total_trades', 0)}\n"
        f"Open: {summary.get('open', 0)}\n"
        f"Closed: {summary.get('closed', 0)}\n"
        f"Total P&L: ${summary.get('total_pnl', 0):+.2f} USDC"
    )
