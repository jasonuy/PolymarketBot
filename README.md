# Polymarket Copy-Trade Bot

Monitors whale wallets on Polymarket and copies their trades automatically.
Runs in **paper trade mode by default** — no real money until you switch it on.

---

## Quick Start

### 1. Install dependencies (first time only)
```bash
cd ~/Polymarket
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Add wallets to watch
Open `config.py` and add wallet addresses to `WATCHED_WALLETS`:
```python
WATCHED_WALLETS = [
    "0xabc123...",   # paste addresses from polymarket.com/leaderboard
]
```

### 3. Start the bot
```bash
cd ~/Polymarket
source venv/bin/activate
python3.13 bot.py
```

### 4. Stop the bot
Press `Ctrl+C`

---

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `WATCHED_WALLETS` | `[]` | Wallet addresses to copy-trade |
| `PAPER_TRADE` | `true` | Log trades without spending real money |
| `POLL_INTERVAL` | `60s` | How often to scan wallets |
| `MAX_TRADE_USDC` | `$50` | Hard cap per trade |
| `MAX_POSITION_FRACTION` | `5%` | Max % of bankroll per trade |
| `MAX_OPEN_POSITIONS` | `10` | Max concurrent open positions |
| `MIN_WHALE_TRADE_USDC` | `$100` | Minimum whale trade size to copy |

---

## Going Live (real money)

1. Copy `.env.example` to `.env`
2. Fill in your wallet private key and Polymarket API keys
3. Set `PAPER_TRADE=false` in `.env`
4. Restart the bot

> **Warning:** Only go live after running paper trade for several weeks and validating the wallet list performs well.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main entry point |
| `config.py` | All settings and risk parameters |
| `api_client.py` | Polymarket API wrapper |
| `wallet_monitor.py` | Detects new whale trades |
| `position_manager.py` | Risk rules and position sizing |
| `trade_executor.py` | Submits orders (paper or live) |
| `database.py` | SQLite logging and P&L tracking |
| `notifier.py` | Telegram alerts |
| `polymarket_bot.db` | Trade log database (auto-created) |
| `bot.log` | Log file (auto-created) |

---

## Telegram Alerts (optional)

1. Message **@BotFather** on Telegram → `/newbot` → copy the token
2. Message **@userinfobot** → copy your chat ID
3. Add both to `.env`:
```
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

---

## Finding Wallets to Watch

Go to **polymarket.com/leaderboard**, click a top trader, and copy their
wallet address from the URL bar. Paste it into `WATCHED_WALLETS` in `config.py`.
