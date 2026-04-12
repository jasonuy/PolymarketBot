# Polymarket Copy Bot — Improvement Roadmap

Current state: bot detects whale trades, copies them with fixed sizing, closes on stop-loss / take-profit / market resolution.

---

## Tier 1 — Highest Impact

### 1. Whale Exit Following
If the wallet we copied exits their position (SELL detected in their activity feed), close our position immediately. Whales often exit before a market resolves badly — this is one of the strongest signals available.

**Files to change:**
- `wallet_monitor.py` — currently filters out SELL trades; instead yield them as a `WhaleSell` event
- `bot.py` — in `check_open_positions()`, check if any open position's source wallet has a matching SELL; if so, close at current price
- `notifier.py` — add `notify_whale_exited()` message

---

### 2. Multi-Whale Confirmation
Only copy a trade if 2+ watched wallets are on the same side of the same market within a short time window (e.g. 10 minutes). Single-whale signals have much higher noise.

**Files to change:**
- `database.py` — add `pending_signals` table: `(market_id, outcome, first_seen_at, wallet_count, wallets)`
- `bot.py` — instead of copying immediately, insert/increment a pending signal row; only execute when `wallet_count >= MIN_WHALE_CONFIRMATION` (new config param) or window expires
- `config.py` — add `MIN_WHALE_CONFIRMATION = int(os.getenv("MIN_WHALE_CONFIRMATION", "2"))` and `CONFIRMATION_WINDOW_SECONDS`
- `dashboard.py` — add `CONFIRMATION_WINDOW_SECONDS` and `MIN_WHALE_CONFIRMATION` to config panel; show pending signals in Position Intelligence section

---

### 3. Trailing Stop-Loss
Instead of a fixed stop from entry, move the stop up as price rises. E.g. if entry=0.40 and price peaks at 0.70, trail at 20% below peak (0.56). Prevents giving back all gains before resolution fires.

**Files to change:**
- `database.py` — add `peak_price REAL` column to `copy_trades`; update it in `check_open_positions()` when current price exceeds it
- `config.py` — add `TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.20"))` (20% pullback from peak)
- `bot.py` — in `check_open_positions()`, update peak price if current > peak; trigger close if current < peak * (1 - TRAILING_STOP_PCT)
- `dashboard.py` — add `TRAILING_STOP_PCT` to config panel

---

### 4. Entry Staleness Check
Don't enter if the current market price has moved more than X% away from the whale's entry price. Prevents copying a trade where you'd be entering at a much worse price than the whale got.

**Files to change:**
- `config.py` — add `MAX_ENTRY_DRIFT_PCT = float(os.getenv("MAX_ENTRY_DRIFT_PCT", "0.15"))` (15% max drift)
- `position_manager.py` — in `should_copy()`, fetch current CLOB price and compare to `trade.price`; skip if drift exceeds threshold
- `dashboard.py` — add `MAX_ENTRY_DRIFT_PCT` to config panel

---

## Tier 2 — High Impact

### 5. Whale Performance Tracking + Auto-Rotation
Track each watched wallet's win rate and P&L contribution independently. Auto-replace underperformers with better wallets from the leaderboard.

**Files to change:**
- `database.py` — add `wallet_stats` table: `(wallet, total_copies, wins, losses, total_pnl, last_updated)`; update on each `close_trade()`
- `bot.py` — after N cycles, call a `rotate_wallets()` function that drops the bottom-performing wallet and fetches a fresh one from `wallet_discovery`
- `dashboard.py` — add a "Whale Performance" table to the Position Intelligence section showing per-wallet stats
- `config.py` — add `WALLET_ROTATION_ENABLED`, `MIN_COPIES_BEFORE_ROTATION`, `MIN_WIN_RATE_TO_KEEP`

---

### 6. Time-Based Position Expiry
Close positions older than X days regardless of outcome. Prevents capital being locked in stagnant markets for months.

**Files to change:**
- `config.py` — add `MAX_POSITION_AGE_DAYS = int(os.getenv("MAX_POSITION_AGE_DAYS", "7"))`
- `bot.py` — in `check_open_positions()`, compare `placed_at` to now; close at current price (or 0.5 if no price available) with reason "Expired"
- `dashboard.py` — add `MAX_POSITION_AGE_DAYS` to config panel

---

### 7. Odds Range Filter
Skip markets outside the 5%–92% probability range. Extreme long shots have terrible expected value; near-certainties have tiny upside.

**Files to change:**
- `config.py` — add `MIN_ODDS = float(os.getenv("MIN_ODDS", "0.05"))` and `MAX_ODDS = float(os.getenv("MAX_ODDS", "0.92"))`
- `position_manager.py` — in `should_copy()`, check `trade.price` against MIN/MAX_ODDS
- `dashboard.py` — add `MIN_ODDS` and `MAX_ODDS` to config panel

---

## Tier 3 — Medium Impact

### 8. Per-Whale Performance Dashboard
Table on the dashboard showing each watched wallet: total signals generated, how many we copied, win rate, total P&L contribution. Makes it easy to see which wallets are actually making money.

**Depends on:** #5 (wallet_stats table)

**Files to change:**
- `dashboard.py` — new `/api/whale_stats` endpoint + HTML table in Position Intelligence section

---

### 9. Conviction-Scaled Sizing
Scale copy size proportionally to the whale's bet size. A whale betting $5,000 is a much stronger signal than a $50 bet. E.g. if whale bets 10x their typical size, you bet 2x your normal size (capped at limits).

**Files to change:**
- `database.py` — need historical average whale bet size per wallet
- `position_manager.py` — add `conviction_multiplier()` that compares `trade.size_usdc` to that wallet's median bet; scale `size_usdc` accordingly (capped at `MAX_TRADE_USDC`)
- `config.py` — add `CONVICTION_SCALING_ENABLED`, `MAX_CONVICTION_MULTIPLIER`

---

### 10. Resolution Date Proximity Preference
Prefer markets resolving within 48 hours over markets resolving in months. Faster resolution = faster capital recycling = more compounding.

**Files to change:**
- `api_client.py` — add `get_market_end_date(condition_id)` using CLOB `/markets/{condition_id}` endpoint (field: `end_date_iso`)
- `position_manager.py` — optionally skip or deprioritize markets with end date > X days away
- `config.py` — add `MAX_MARKET_DAYS_TO_RESOLUTION = int(os.getenv("MAX_MARKET_DAYS_TO_RESOLUTION", "30"))`

---

## Tier 4 — Infrastructure

### 11. WebSocket Feed
Replace 60-second polling with Polymarket's WebSocket feed for near-instant signal detection (~1s vs up to 60s). In fast-moving markets this is the difference between copying at 0.35 and 0.60.

**Files to change:**
- `wallet_monitor.py` — add WebSocket client (using `websockets` or `asyncio`) alongside existing polling fallback
- `bot.py` — restructure main loop to be async or use threading for WS listener

---

### 12. Backtesting Engine
Feed historical whale activity through current bot logic and see what P&L would have been. Lets you tune stop-loss/take-profit/min-size parameters with data.

**New file:** `backtest.py`
- Replay `whale_trades` table through `position_manager.should_copy()` and `check_open_positions()` logic
- Output a simulated P&L curve with configurable parameters
- Compare different parameter sets side by side

---

## Suggested Implementation Order

| Priority | Feature | Effort | Expected Impact |
|----------|---------|--------|----------------|
| 1 | Whale Exit Following (#1) | Low | High |
| 2 | Entry Staleness Check (#4) | Low | High |
| 3 | Trailing Stop-Loss (#3) | Low | High |
| 4 | Odds Range Filter (#7) | Very Low | Medium |
| 5 | Time-Based Expiry (#6) | Very Low | Medium |
| 6 | Multi-Whale Confirmation (#2) | Medium | Very High |
| 7 | Whale Performance + Auto-Rotation (#5) | Medium | High |
| 8 | Per-Whale Dashboard (#8) | Low | Medium |
| 9 | Conviction-Scaled Sizing (#9) | Medium | Medium |
| 10 | Resolution Date Proximity (#10) | Low | Medium |
| 11 | WebSocket Feed (#11) | High | Medium |
| 12 | Backtesting Engine (#12) | High | Medium |
