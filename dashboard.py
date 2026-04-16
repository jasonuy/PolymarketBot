"""
Polymarket Copy-Bot Web Dashboard
──────────────────────────────────
Run with:
    source venv/bin/activate
    python dashboard.py

Then open http://localhost:5000 in your browser.
For internet access, use ngrok:
    ngrok http 5000
"""

import base64
from flask import Flask, jsonify, Response, request
import sqlite3
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from config import DB_PATH, PAPER_TRADE, MAX_TRADE_USDC, LIVE_BANKROLL, FUNDER_ADDRESS
import api_client as _api

LOG_PATH  = "bot.log"
ENV_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
LOG_TAIL_LINES = 100

app = Flask(__name__)

STARTING_BANKROLL = LIVE_BANKROLL if LIVE_BANKROLL > 0 else MAX_TRADE_USDC * 20

# ── Dashboard auth ────────────────────────────────────────────────────────────
# Set DASHBOARD_TOKEN in .env to require a password for all dashboard access.
# Any username, password = DASHBOARD_TOKEN.  Leave blank to disable (local-only).
_DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

@app.before_request
def _require_auth():
    if not _DASHBOARD_TOKEN:
        return  # no auth configured — local-only use
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
            if pwd == _DASHBOARD_TOKEN:
                return
        except Exception:
            pass
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Polymarket Dashboard"'},
    )


# ── Config helpers ────────────────────────────────────────────────────────────

# Keys that the dashboard Settings panel can read and write.
CONFIG_DEFAULTS = {
    "PAPER_TRADE":           "true",
    "POLL_INTERVAL_SECONDS": "60",
    "LIVE_BANKROLL":         "0.0",
    "MIN_WHALE_TRADE_USDC":  "5.0",
    "MAX_TRADE_USDC":        "50.0",
    "MAX_POSITION_FRACTION": "0.05",
    "MAX_OPEN_POSITIONS":    "10",
    "MAX_SPREAD_PCT":        "0.10",
    "STOP_LOSS_PCT":         "0.50",
    "TAKE_PROFIT_PCT":       "0.80",
}

# Keys that must NEVER be returned by any API endpoint or logged anywhere.
_SENSITIVE_KEYS = {
    "PRIVATE_KEY", "POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WALLET_ADDRESS", "DASHBOARD_TOKEN",
}


def read_env() -> dict:
    """
    Read .env and return only the safe, configurable keys.
    Sensitive keys (PRIVATE_KEY, API credentials, etc.) are never included.
    """
    values = dict(CONFIG_DEFAULTS)
    try:
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k in CONFIG_DEFAULTS:   # whitelist — only safe keys
                        values[k] = v.strip()
    except FileNotFoundError:
        pass
    return values


def write_env(updates: dict) -> None:
    """
    Merge updated config values into .env, preserving every existing line
    (including sensitive keys the dashboard never sees).
    Only keys in CONFIG_DEFAULTS are accepted from updates.
    """
    # Read existing file line-by-line so we can do in-place updates
    existing_lines: list[str] = []
    key_to_line: dict[str, int] = {}
    try:
        with open(ENV_PATH, "r") as f:
            existing_lines = [ln.rstrip("\n") for ln in f]
    except FileNotFoundError:
        pass

    for i, line in enumerate(existing_lines):
        stripped = line.strip()
        if stripped and "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            key_to_line[k] = i

    # Apply only safe updates
    safe_updates = {k: v for k, v in updates.items() if k in CONFIG_DEFAULTS}
    for k, v in safe_updates.items():
        if k in key_to_line:
            existing_lines[key_to_line[k]] = f"{k}={v}"
        else:
            existing_lines.append(f"{k}={v}")

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(existing_lines) + "\n")


def restart_bot():
    """Kill any running bot.py process and start a fresh one."""
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    python  = sys.executable

    if sys.platform == "win32":
        subprocess.run(
            'wmic process where "commandline like \'%bot.py%\'" call terminate',
            shell=True, capture_output=True
        )
        time.sleep(1)
        subprocess.Popen(
            [python, os.path.join(bot_dir, "bot.py")],
            cwd=bot_dir,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        # macOS / Linux
        subprocess.run(["pkill", "-f", "bot.py"], capture_output=True)
        time.sleep(1)
        log_path = os.path.join(bot_dir, "bot.log")
        with open(log_path, "a") as log_f:
            subprocess.Popen(
                [python, os.path.join(bot_dir, "bot.py")],
                cwd=bot_dir,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )


def query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def query_one(sql: str, params: tuple = ()) -> dict:
    rows = query(sql, params)
    return rows[0] if rows else {}


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(read_env())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    current = read_env()
    for key in CONFIG_DEFAULTS:
        if key in data:
            current[key] = str(data[key])
    write_env(current)
    restart_bot()
    return jsonify({"ok": True})


@app.route("/api/summary")
def api_summary():
    row = query_one("""
        SELECT
            COUNT(*)                                                        AS total_trades,
            COALESCE(SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END), 0)  AS open_trades,
            COALESCE(SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END), 0)  AS closed_trades,
            COALESCE(SUM(CASE WHEN status='CLOSED' AND pnl_usdc > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN status='CLOSED' AND pnl_usdc < 0 THEN 1 ELSE 0 END), 0) AS losses,
            COALESCE(ROUND(SUM(COALESCE(pnl_usdc, 0)), 2), 0)              AS total_pnl,
            COALESCE(ROUND(SUM(CASE WHEN status='OPEN' THEN size_usdc ELSE 0 END), 2), 0) AS deployed_usdc
        FROM copy_trades
        WHERE paper_trade = 0
    """)
    closed = row.get("closed_trades", 0) or 0
    wins   = row.get("wins", 0) or 0
    row["win_rate"]   = round(wins / closed * 100, 1) if closed > 0 else 0
    row["paper_mode"] = PAPER_TRADE

    live_bankroll = float(read_env().get("LIVE_BANKROLL", "0") or "0")
    row["starting_balance"] = live_bankroll if live_bankroll > 0 else None

    if FUNDER_ADDRESS:
        try:
            import trade_executor
            cash = trade_executor.get_live_balance()
            portfolio = _api.get_portfolio(FUNDER_ADDRESS)
            row["cash"]            = cash
            row["positions_value"] = portfolio["positions_value"]
            row["portfolio_value"] = round(cash + portfolio["positions_value"], 2)

            # Calculate starting balance from trade history (net deposits).
            # Fall back to LIVE_BANKROLL env var if the API call fails.
            calculated = _api.get_starting_balance(FUNDER_ADDRESS, cash)
            if calculated is not None:
                row["starting_balance"] = calculated
            elif live_bankroll > 0:
                row["starting_balance"] = live_bankroll

            sb = row["starting_balance"]
            row["total_pnl"] = round(row["portfolio_value"] - sb, 2) if sb else None
        except Exception:
            row["cash"]            = None
            row["positions_value"] = None
            row["portfolio_value"] = None
            row["total_pnl"]       = None
    else:
        row["cash"]            = None
        row["positions_value"] = None
        row["portfolio_value"] = None
        row["total_pnl"]       = None

    row["whale_count"] = (query_one("SELECT COUNT(*) AS n FROM whale_trades") or {}).get("n", 0)
    return jsonify(row)


@app.route("/api/trades")
def api_trades():
    rows = query("""
        SELECT
            ct.id,
            ct.placed_at,
            ct.closed_at,
            ct.market_id,
            wt.market_question,
            ct.outcome,
            ct.side,
            ROUND(ct.price, 4)       AS entry_price,
            ROUND(ct.close_price, 4) AS exit_price,
            ROUND(ct.size_usdc, 2)   AS size_usdc,
            ROUND(ct.pnl_usdc, 2)    AS pnl_usdc,
            ct.status,
            ct.paper_trade
        FROM copy_trades ct
        LEFT JOIN whale_trades wt ON ct.whale_trade_id = wt.id
        ORDER BY ct.placed_at DESC
        LIMIT 100
    """)
    return jsonify(rows)


@app.route("/api/whales")
def api_whales():
    rows = query("""
        SELECT
            detected_at,
            SUBSTR(wallet, 1, 8) || '...' || SUBSTR(wallet, -6) AS wallet_short,
            wallet,
            market_question,
            outcome,
            side,
            ROUND(price, 4)    AS price,
            ROUND(size_usdc, 2) AS size_usdc
        FROM whale_trades
        ORDER BY detected_at DESC
        LIMIT 50
    """)
    return jsonify(rows)


@app.route("/api/log")
def api_log():
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in lines[-LOG_TAIL_LINES:]]})
    except FileNotFoundError:
        return jsonify({"lines": ["bot.log not found"]})


@app.route("/api/insights")
def api_insights():
    # Open positions with whale signal count since entry
    positions = query("""
        SELECT
            ct.id,
            COALESCE(ct.market_question,
                (SELECT wt.market_question FROM whale_trades wt
                 WHERE wt.market_id = ct.market_id LIMIT 1),
                ct.market_id) AS market_question,
            ct.market_id,
            ct.outcome,
            ROUND(ct.price, 3) AS entry_price,
            ct.placed_at,
            ROUND(ct.size_usdc, 2) AS size_usdc,
            (SELECT COUNT(*) FROM whale_trades wt2
             WHERE wt2.market_id = ct.market_id
               AND wt2.detected_at >= ct.placed_at) AS signals_since_entry
        FROM copy_trades ct
        WHERE ct.status = 'OPEN'
        ORDER BY ct.placed_at DESC
    """)

    # Whale activity grouped by market in the last 3 hours
    activity = query("""
        SELECT
            COALESCE(market_question, market_id) AS market_question,
            market_id,
            COUNT(*) AS signal_count,
            ROUND(SUM(size_usdc), 0) AS total_whale_usdc,
            COUNT(DISTINCT wallet) AS whale_count,
            MAX(detected_at) AS last_seen
        FROM whale_trades
        WHERE detected_at > datetime('now', '-3 hours')
        GROUP BY market_id
        ORDER BY signal_count DESC
        LIMIT 10
    """)

    # Enrich open positions with live Data API values (cur price, value, P&L)
    if FUNDER_ADDRESS:
        try:
            portfolio = _api.get_portfolio(FUNDER_ADDRESS)
            by_cond = portfolio["by_condition"]
            for p in positions:
                live = by_cond.get((p.get("market_id") or "").lower())
                if live:
                    p["cur_price"]   = live.get("curPrice")
                    p["cur_value"]   = round(live.get("currentValue", 0), 2)
                    p["pnl_usdc"]    = round(live.get("cashPnl", 0), 2)
                    p["pnl_pct"]     = round(live.get("percentPnl", 0), 1)
                else:
                    p["cur_price"] = p["cur_value"] = p["pnl_usdc"] = p["pnl_pct"] = None
        except Exception:
            pass

    held_ids = {p["market_id"] for p in positions}
    for row in activity:
        row["we_hold"] = row["market_id"] in held_ids

    total_signals  = sum(r["signal_count"] for r in activity)
    signals_in_held = sum(r["signal_count"] for r in activity if r["we_hold"])
    cfg = read_env()
    max_pos   = int(cfg.get("MAX_OPEN_POSITIONS", "10"))
    open_count = len(positions)

    return jsonify({
        "positions": positions,
        "activity":  activity,
        "stats": {
            "open_count":             open_count,
            "max_positions":          max_pos,
            "slots_free":             max(0, max_pos - open_count),
            "total_signals_3h":       total_signals,
            "signals_in_held_markets": signals_in_held,
            "pct_held": round(signals_in_held / total_signals * 100) if total_signals > 0 else 0,
        },
    })


@app.route("/api/stats")
def api_stats():
    """Full stats report for nightly optimization sessions."""
    import database as _db
    return jsonify(_db.get_stats_report())


@app.route("/api/pnl_over_time")
def api_pnl_over_time():
    rows = query("""
        SELECT
            closed_at AS date,
            ROUND(SUM(pnl_usdc) OVER (ORDER BY closed_at), 2) AS cumulative_pnl
        FROM copy_trades
        WHERE status = 'CLOSED' AND closed_at IS NOT NULL
        ORDER BY closed_at
    """)
    return jsonify(rows)


@app.route("/api/balance_history")
def api_balance_history():
    period = request.args.get("period", "days")
    now = datetime.now(timezone.utc)

    if period == "hours":
        buckets = [now - timedelta(hours=i) for i in range(24, -1, -1)]
        fmt = lambda dt: dt.strftime("%H:%M")
    elif period == "weeks":
        buckets = [now - timedelta(weeks=i) for i in range(11, -1, -1)]
        fmt = lambda dt: dt.strftime("%b %d")
    else:  # days
        buckets = [now - timedelta(days=i) for i in range(29, -1, -1)]
        fmt = lambda dt: dt.strftime("%b %d")

    trades = query("""
        SELECT closed_at, pnl_usdc
        FROM copy_trades
        WHERE status='CLOSED' AND paper_trade=0 AND closed_at IS NOT NULL
        ORDER BY closed_at
    """)

    # Use dynamically calculated starting balance (same as /api/summary)
    try:
        import trade_executor as _te
        cash = _te.get_live_balance()
        starting = _api.get_starting_balance(FUNDER_ADDRESS, cash) if FUNDER_ADDRESS else None
    except Exception:
        starting = None
    if not starting:
        live_bankroll = float(read_env().get("LIVE_BANKROLL", "0") or "0")
        starting = live_bankroll if live_bankroll > 0 else 0.0

    result = []
    for bucket in buckets:
        bucket_iso = bucket.isoformat()
        cumulative_pnl = sum(
            t["pnl_usdc"] for t in trades
            if t["pnl_usdc"] is not None and t["closed_at"] <= bucket_iso
        )
        result.append({
            "label": fmt(bucket),
            "balance": round(starting + cumulative_pnl, 2),
        })

    return jsonify(result)


# ── Documentation HTML ────────────────────────────────────────────────────────

DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bot Documentation — Polymarket Copy-Bot</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px; line-height: 1.6; }
    a { color: #4d9fff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .nav { display: flex; align-items: center; gap: 16px; background: #1a1d27;
           border-bottom: 1px solid #2a2d3a; padding: 14px 24px; }
    .nav-back { color: #4d9fff; font-size: 0.88rem; }
    .nav-brand { font-size: 1.1rem; font-weight: 700; }
    .page { max-width: 960px; margin: 0 auto; padding: 28px 24px; }
    h1 { font-size: 1.6rem; font-weight: 700; margin-bottom: 6px; }
    h2 { font-size: 1.05rem; font-weight: 700; color: #c0c4d8; margin: 32px 0 12px;
         padding-bottom: 6px; border-bottom: 1px solid #2a2d3a; }
    p { color: #b0b4c8; margin-bottom: 12px; }
    .lead { font-size: 1rem; color: #c0c4d8; margin-bottom: 24px; }
    .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 18px 20px; }
    /* Pipeline */
    .pipeline { display: flex; align-items: center; gap: 0; margin: 20px 0; flex-wrap: wrap; }
    .pipe-step { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 8px;
                 padding: 12px 14px; text-align: center; min-width: 110px; flex: 1; }
    .pipe-step .num { font-size: 0.62rem; color: #4d9fff; font-weight: 700; text-transform: uppercase;
                      letter-spacing: 1px; margin-bottom: 4px; }
    .pipe-step .lbl { font-size: 0.82rem; font-weight: 600; color: #e0e0e0; }
    .pipe-arrow { color: #444; font-size: 1.2rem; padding: 0 6px; flex-shrink: 0; }
    @media(max-width:700px) { .pipeline { flex-direction: column; }
      .pipe-arrow { transform: rotate(90deg); margin: 4px 0; } }
    /* Module grid */
    .mod-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }
    @media(max-width:800px) { .mod-grid { grid-template-columns: 1fr 1fr; } }
    @media(max-width:500px) { .mod-grid { grid-template-columns: 1fr; } }
    .mod-card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 8px; padding: 14px; }
    .mod-name { font-size: 0.75rem; font-weight: 700; color: #4d9fff; font-family: monospace; margin-bottom: 6px; }
    .mod-desc { font-size: 0.78rem; color: #8b8fa8; line-height: 1.5; }
    /* Diagrams */
    .diagram-wrap { background: #13151f; border: 1px solid #2a2d3a; border-radius: 10px;
                    padding: 20px; margin: 16px 0; overflow-x: auto; text-align: center; }
    .diagram-wrap svg { display: inline-block; }
    /* Tables */
    .cfg-tbl { width: 100%; border-collapse: collapse; margin: 12px 0; }
    .cfg-tbl th { color: #8b8fa8; font-size: 0.72rem; text-transform: uppercase; padding: 7px 10px;
                  border-bottom: 1px solid #2a2d3a; text-align: left; }
    .cfg-tbl td { padding: 8px 10px; border-bottom: 1px solid #1e2130; font-size: 0.82rem; vertical-align: top; }
    .cfg-tbl tr:hover td { background: #22253a; }
    .mono { font-family: monospace; font-size: 0.8rem; color: #4d9fff; }
    .def { color: #8b8fa8; font-family: monospace; font-size: 0.78rem; }
    /* Compare */
    .cmp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 16px 0; }
    @media(max-width:600px) { .cmp-grid { grid-template-columns: 1fr; } }
    .cmp-card { border-radius: 10px; padding: 18px 20px; }
    .cmp-paper { background: #1a1a00; border: 1px solid #3d3a00; }
    .cmp-live  { background: #001a0d; border: 1px solid #003d1a; }
    .cmp-title { font-size: 1rem; font-weight: 700; margin-bottom: 10px; }
    .cmp-paper .cmp-title { color: #ffd700; }
    .cmp-live  .cmp-title { color: #00c49a; }
    .cmp-list { list-style: none; }
    .cmp-list li { font-size: 0.83rem; color: #b0b4c8; padding: 3px 0; }
    .cmp-list li::before { content: "• "; color: #555; }
  </style>
</head>
<body>

<div class="nav">
  <a href="/" class="nav-back">&#8592; Dashboard</a>
  <span class="nav-brand">Documentation</span>
</div>

<div class="page">

  <h1>How the Copy-Bot Works</h1>
  <p class="lead">A guide to the algorithm, modules, and configuration parameters driving the Polymarket copy-trading bot.</p>

  <h2>The 5-Step Pipeline</h2>
  <div class="pipeline">
    <div class="pipe-step"><div class="num">Step 1</div><div class="lbl">Discover Whales</div></div>
    <div class="pipe-arrow">&#9654;</div>
    <div class="pipe-step"><div class="num">Step 2</div><div class="lbl">Monitor Activity</div></div>
    <div class="pipe-arrow">&#9654;</div>
    <div class="pipe-step"><div class="num">Step 3</div><div class="lbl">Evaluate Signal</div></div>
    <div class="pipe-arrow">&#9654;</div>
    <div class="pipe-step"><div class="num">Step 4</div><div class="lbl">Copy Trade</div></div>
    <div class="pipe-arrow">&#9654;</div>
    <div class="pipe-step"><div class="num">Step 5</div><div class="lbl">Exit &amp; Record</div></div>
  </div>
  <p>Every <strong>POLL_INTERVAL</strong> seconds the bot polls each watched wallet for new trades, applies risk filters to any new signal, executes qualifying copy trades, then checks every open position for exit conditions.</p>

  <h2>Main Bot Loop</h2>
  <p>The bot runs an infinite loop. Each cycle starts by checking existing positions for exit signals, then scans watched wallets for new trades.</p>
  <div class="diagram-wrap">
    <svg viewBox="0 0 520 390" width="520" height="390" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="ml-a" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#4d9fff"/>
        </marker>
        <marker id="ml-l" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#555"/>
        </marker>
      </defs>
      <ellipse cx="260" cy="32" rx="74" ry="24" fill="#003d1a" stroke="#00c49a" stroke-width="1.5"/>
      <text x="260" y="37" text-anchor="middle" fill="#00c49a" font-size="13" font-weight="700">START</text>
      <line x1="260" y1="56" x2="260" y2="73" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ml-a)"/>
      <rect x="160" y="75" width="200" height="40" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="260" y="100" text-anchor="middle" fill="#e0e0e0" font-size="12">Init DB &amp; Wallets</text>
      <line x1="260" y1="115" x2="260" y2="133" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ml-a)"/>
      <rect x="85" y="129" width="350" height="236" rx="8" fill="none" stroke="#2a2d3a" stroke-width="1" stroke-dasharray="5,4"/>
      <text x="93" y="143" fill="#3a3a5a" font-size="9" font-style="italic">LOOP</text>
      <rect x="160" y="145" width="200" height="40" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="260" y="170" text-anchor="middle" fill="#e0e0e0" font-size="12">Check Open Positions</text>
      <line x1="260" y1="185" x2="260" y2="203" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ml-a)"/>
      <rect x="160" y="205" width="200" height="40" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="260" y="230" text-anchor="middle" fill="#e0e0e0" font-size="12">Scan Whale Wallets</text>
      <line x1="260" y1="245" x2="260" y2="263" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ml-a)"/>
      <rect x="160" y="265" width="200" height="40" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="260" y="290" text-anchor="middle" fill="#e0e0e0" font-size="12">Process New Signals</text>
      <line x1="260" y1="305" x2="260" y2="323" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ml-a)"/>
      <rect x="160" y="325" width="200" height="40" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="260" y="350" text-anchor="middle" fill="#e0e0e0" font-size="12">Sleep POLL_INTERVAL s</text>
      <path d="M 160,345 H 100 V 165 H 160" fill="none" stroke="#555" stroke-width="1.5" stroke-dasharray="4,3" marker-end="url(#ml-l)"/>
    </svg>
  </div>

  <h2>Trade Entry Decision</h2>
  <p>When a new whale trade is detected, it runs through a series of risk filters before the bot decides to copy it.</p>
  <div class="diagram-wrap">
    <svg viewBox="0 0 640 530" width="640" height="530" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="en-a" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#4d9fff"/>
        </marker>
        <marker id="en-n" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#ff4d6d"/>
        </marker>
        <marker id="en-y" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#00c49a"/>
        </marker>
      </defs>
      <rect x="220" y="10" width="200" height="40" rx="6" fill="#0d2040" stroke="#4d9fff" stroke-width="1.5"/>
      <text x="320" y="35" text-anchor="middle" fill="#4d9fff" font-size="12" font-weight="700">Whale Trade Detected</text>
      <line x1="320" y1="50" x2="320" y2="68" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#en-a)"/>
      <polygon points="320,70 415,113 320,156 225,113" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="109" text-anchor="middle" fill="#e0e0e0" font-size="11">Max positions</text>
      <text x="320" y="123" text-anchor="middle" fill="#e0e0e0" font-size="11">reached?</text>
      <line x1="320" y1="156" x2="320" y2="174" stroke="#00c49a" stroke-width="1.5" marker-end="url(#en-y)"/>
      <text x="328" y="170" fill="#00c49a" font-size="10">No</text>
      <line x1="415" y1="113" x2="488" y2="113" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#en-n)"/>
      <text x="424" y="107" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="490" y="97" width="116" height="32" rx="5" fill="#3d0d1a" stroke="#ff4d6d" stroke-width="1"/>
      <text x="548" y="117" text-anchor="middle" fill="#ff4d6d" font-size="11">SKIP trade</text>
      <polygon points="320,176 415,219 320,262 225,219" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="215" text-anchor="middle" fill="#e0e0e0" font-size="11">Already in</text>
      <text x="320" y="229" text-anchor="middle" fill="#e0e0e0" font-size="11">this market?</text>
      <line x1="320" y1="262" x2="320" y2="280" stroke="#00c49a" stroke-width="1.5" marker-end="url(#en-y)"/>
      <text x="328" y="276" fill="#00c49a" font-size="10">No</text>
      <line x1="415" y1="219" x2="488" y2="219" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#en-n)"/>
      <text x="424" y="213" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="490" y="203" width="116" height="32" rx="5" fill="#3d0d1a" stroke="#ff4d6d" stroke-width="1"/>
      <text x="548" y="223" text-anchor="middle" fill="#ff4d6d" font-size="11">SKIP trade</text>
      <polygon points="320,282 415,325 320,368 225,325" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="321" text-anchor="middle" fill="#e0e0e0" font-size="11">Spread &gt;</text>
      <text x="320" y="335" text-anchor="middle" fill="#e0e0e0" font-size="11">MAX_SPREAD?</text>
      <line x1="320" y1="368" x2="320" y2="386" stroke="#00c49a" stroke-width="1.5" marker-end="url(#en-y)"/>
      <text x="328" y="382" fill="#00c49a" font-size="10">No</text>
      <line x1="415" y1="325" x2="488" y2="325" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#en-n)"/>
      <text x="424" y="319" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="490" y="309" width="116" height="32" rx="5" fill="#3d0d1a" stroke="#ff4d6d" stroke-width="1"/>
      <text x="548" y="329" text-anchor="middle" fill="#ff4d6d" font-size="11">SKIP trade</text>
      <polygon points="320,388 415,426 320,464 225,426" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="422" text-anchor="middle" fill="#e0e0e0" font-size="11">Computed size</text>
      <text x="320" y="436" text-anchor="middle" fill="#e0e0e0" font-size="11">&lt; $1 USDC?</text>
      <line x1="320" y1="464" x2="320" y2="482" stroke="#00c49a" stroke-width="1.5" marker-end="url(#en-y)"/>
      <text x="328" y="478" fill="#00c49a" font-size="10">No</text>
      <line x1="415" y1="426" x2="488" y2="426" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#en-n)"/>
      <text x="424" y="420" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="490" y="410" width="116" height="32" rx="5" fill="#3d0d1a" stroke="#ff4d6d" stroke-width="1"/>
      <text x="548" y="430" text-anchor="middle" fill="#ff4d6d" font-size="11">SKIP trade</text>
      <rect x="210" y="484" width="220" height="42" rx="6" fill="#003d1a" stroke="#00c49a" stroke-width="1.5"/>
      <text x="320" y="505" text-anchor="middle" fill="#00c49a" font-size="12" font-weight="700">EXECUTE Copy Trade</text>
      <text x="320" y="519" text-anchor="middle" fill="#00c49a" font-size="10">Record in DB + Telegram alert</text>
    </svg>
  </div>

  <h2>Position Exit Logic</h2>
  <p>Every cycle, each open position is checked against three exit conditions in order. The first condition that triggers closes the position and records P&amp;L.</p>
  <div class="diagram-wrap">
    <svg viewBox="0 0 640 480" width="640" height="480" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <marker id="ex-a" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#4d9fff"/>
        </marker>
        <marker id="ex-n" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#ff4d6d"/>
        </marker>
        <marker id="ex-y" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <polygon points="0 0,8 3,0 6" fill="#00c49a"/>
        </marker>
      </defs>
      <rect x="200" y="10" width="240" height="40" rx="6" fill="#0d2040" stroke="#4d9fff" stroke-width="1.5"/>
      <text x="320" y="35" text-anchor="middle" fill="#4d9fff" font-size="12" font-weight="700">Open Position (each cycle)</text>
      <line x1="320" y1="50" x2="320" y2="68" stroke="#4d9fff" stroke-width="1.5" marker-end="url(#ex-a)"/>
      <polygon points="320,70 425,113 320,156 215,113" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="109" text-anchor="middle" fill="#e0e0e0" font-size="11">Price dropped &gt;=</text>
      <text x="320" y="123" text-anchor="middle" fill="#e0e0e0" font-size="11">STOP_LOSS_PCT?</text>
      <line x1="320" y1="156" x2="320" y2="174" stroke="#00c49a" stroke-width="1.5" marker-end="url(#ex-y)"/>
      <text x="328" y="170" fill="#00c49a" font-size="10">No</text>
      <line x1="425" y1="113" x2="490" y2="113" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#ex-n)"/>
      <text x="433" y="107" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="492" y="97" width="128" height="32" rx="5" fill="#3d0d1a" stroke="#ff4d6d" stroke-width="1"/>
      <text x="556" y="117" text-anchor="middle" fill="#ff4d6d" font-size="11">CLOSE — Stop-loss</text>
      <polygon points="320,176 425,219 320,262 215,219" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="215" text-anchor="middle" fill="#e0e0e0" font-size="11">Price rose &gt;=</text>
      <text x="320" y="229" text-anchor="middle" fill="#e0e0e0" font-size="11">TAKE_PROFIT_PCT?</text>
      <line x1="320" y1="262" x2="320" y2="280" stroke="#00c49a" stroke-width="1.5" marker-end="url(#ex-y)"/>
      <text x="328" y="276" fill="#00c49a" font-size="10">No</text>
      <line x1="425" y1="219" x2="490" y2="219" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#ex-n)"/>
      <text x="433" y="213" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="492" y="203" width="128" height="32" rx="5" fill="#003d1a" stroke="#00c49a" stroke-width="1"/>
      <text x="556" y="223" text-anchor="middle" fill="#00c49a" font-size="11">CLOSE — Take-profit</text>
      <polygon points="320,282 425,320 320,358 215,320" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="316" text-anchor="middle" fill="#e0e0e0" font-size="11">Market</text>
      <text x="320" y="330" text-anchor="middle" fill="#e0e0e0" font-size="11">resolved?</text>
      <line x1="320" y1="358" x2="320" y2="376" stroke="#00c49a" stroke-width="1.5" marker-end="url(#ex-y)"/>
      <text x="328" y="372" fill="#00c49a" font-size="10">No</text>
      <line x1="425" y1="320" x2="490" y2="320" stroke="#ff4d6d" stroke-width="1.5" marker-end="url(#ex-n)"/>
      <text x="433" y="314" fill="#ff4d6d" font-size="10">Yes</text>
      <rect x="492" y="304" width="128" height="32" rx="5" fill="#1a1d27" stroke="#4d9fff" stroke-width="1"/>
      <text x="556" y="324" text-anchor="middle" fill="#4d9fff" font-size="11">CLOSE — Resolved</text>
      <text x="556" y="336" text-anchor="middle" fill="#555" font-size="9">1.0 = WIN  |  0.0 = LOSS</text>
      <rect x="220" y="378" width="200" height="38" rx="6" fill="#1a1d27" stroke="#2a2d3a" stroke-width="1.5"/>
      <text x="320" y="402" text-anchor="middle" fill="#8b8fa8" font-size="12">HOLD — check next cycle</text>
      <line x1="320" y1="416" x2="320" y2="434" stroke="#444" stroke-width="1.5" marker-end="url(#ex-a)"/>
      <rect x="220" y="436" width="200" height="32" rx="6" fill="#13151f" stroke="#2a2d3a" stroke-width="1"/>
      <text x="320" y="457" text-anchor="middle" fill="#555" font-size="11">Sleep POLL_INTERVAL &#8594; repeat</text>
    </svg>
  </div>

  <h2>Module Architecture</h2>
  <div class="mod-grid">
    <div class="mod-card">
      <div class="mod-name">bot.py</div>
      <div class="mod-desc">Main entry point. Runs the polling loop, orchestrates all other modules, and handles startup.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">wallet_monitor.py</div>
      <div class="mod-desc">Polls the Polymarket Data API for new BUY trades from watched wallets. Tracks seen tx hashes to avoid duplicates.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">position_manager.py</div>
      <div class="mod-desc">Applies risk rules to decide whether to copy a trade and how large the position should be.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">trade_executor.py</div>
      <div class="mod-desc">Executes trades — paper mode logs them instantly, live mode sends signed orders to the CLOB API.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">api_client.py</div>
      <div class="mod-desc">Thin wrapper over Polymarket's Data, Gamma, and CLOB REST APIs. Handles retries and error logging.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">database.py</div>
      <div class="mod-desc">SQLite persistence layer. Stores whale trades, copy trades, and P&amp;L history.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">notifier.py</div>
      <div class="mod-desc">Sends Telegram alerts for every significant event — trade detected, copied, closed, or error.</div>
    </div>
    <div class="mod-card">
      <div class="mod-name">wallet_discovery.py</div>
      <div class="mod-desc">Auto-discovers high-performing wallets from the Polymarket leaderboard when no wallets are manually configured.</div>
    </div>
  </div>

  <h2>Configuration Reference</h2>
  <table class="cfg-tbl">
    <thead><tr><th>Parameter</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td class="mono">PAPER_TRADE</td><td class="def">true</td><td>Simulation mode — no real money is spent. Recommended until logic is validated.</td></tr>
      <tr><td class="mono">POLL_INTERVAL_SECONDS</td><td class="def">60</td><td>How often (seconds) the bot polls each watched wallet for new trades.</td></tr>
      <tr><td class="mono">MIN_WHALE_TRADE_USDC</td><td class="def">5.0</td><td>Minimum whale bet size to qualify as a signal. Filters out noise from small speculative trades.</td></tr>
      <tr><td class="mono">MAX_TRADE_USDC</td><td class="def">50.0</td><td>Hard ceiling on the USDC spent per copied trade regardless of bankroll size.</td></tr>
      <tr><td class="mono">MAX_POSITION_FRACTION</td><td class="def">0.05</td><td>Maximum fraction of the bankroll to risk per trade. 0.05 = 5%, so $50 on a $1,000 bankroll.</td></tr>
      <tr><td class="mono">MAX_OPEN_POSITIONS</td><td class="def">10</td><td>Maximum simultaneous open trades. New signals are skipped when the limit is reached.</td></tr>
      <tr><td class="mono">MAX_SPREAD_PCT</td><td class="def">0.10</td><td>Skip markets where bid/ask spread exceeds this fraction. Only enforced in live mode.</td></tr>
      <tr><td class="mono">STOP_LOSS_PCT</td><td class="def">0.50</td><td>Close a position if current price falls 50% below entry. Limits downside on losing trades.</td></tr>
      <tr><td class="mono">TAKE_PROFIT_PCT</td><td class="def">0.80</td><td>Close a position if current price rises 80% above entry. Locks in gains before reversal.</td></tr>
    </tbody>
  </table>

  <h2>Paper Mode vs Live Mode</h2>
  <div class="cmp-grid">
    <div class="cmp-card cmp-paper">
      <div class="cmp-title">Paper Trade (PAPER_TRADE=true)</div>
      <ul class="cmp-list">
        <li>No real USDC is spent — all trades are simulated</li>
        <li>Uses a synthetic bankroll of MAX_TRADE_USDC &times; 20</li>
        <li>Spread check is skipped (no real execution risk)</li>
        <li>P&amp;L is calculated as if trades were real</li>
        <li>Safe to run without wallet credentials</li>
        <li>Recommended for initial validation</li>
      </ul>
    </div>
    <div class="cmp-card cmp-live">
      <div class="cmp-title">Live Trade (PAPER_TRADE=false)</div>
      <ul class="cmp-list">
        <li>Real USDC is spent via the Polymarket CLOB API</li>
        <li>Uses actual wallet balance as bankroll</li>
        <li>Spread check is enforced to avoid bad fills</li>
        <li>Requires PRIVATE_KEY and API credentials in .env</li>
        <li>All risk rules apply identically to paper mode</li>
        <li>Only enable after paper trading shows positive results</li>
      </ul>
    </div>
  </div>

  <h2>APIs Used</h2>
  <table class="cfg-tbl">
    <thead><tr><th>Name</th><th>Base URL</th><th>Used For</th></tr></thead>
    <tbody>
      <tr><td class="mono">Data API</td><td class="def">data-api.polymarket.com</td><td>Wallet activity (trades) and current positions</td></tr>
      <tr><td class="mono">Gamma API</td><td class="def">gamma-api.polymarket.com</td><td>Market metadata and prices</td></tr>
      <tr><td class="mono">CLOB API</td><td class="def">clob.polymarket.com</td><td>Live prices, order book, balance, market resolution, trade execution</td></tr>
    </tbody>
  </table>

</div>
</body>
</html>
"""


@app.route("/docs")
def docs():
    return Response(DOCS_HTML, mimetype='text/html')


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy-Bot Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px; }
    a { color: inherit; }
    /* Nav */
    .nav { display: flex; align-items: center; gap: 12px; background: #1a1d27;
           border-bottom: 1px solid #2a2d3a; padding: 14px 24px; }
    .nav-brand { font-size: 1.1rem; font-weight: 700; }
    .nav-right { margin-left: auto; font-size: 0.72rem; color: #666; }
    /* Badge */
    .badge { padding: 3px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; }
    .badge-paper { background: #3d3a00; color: #ffd700; }
    .badge-live  { background: #003d1a; color: #00ff88; }
    /* Layout */
    .page { padding: 20px 24px; }
    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
    .grid-2 { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; margin-bottom: 16px; }
    @media(max-width:800px) { .grid-4 { grid-template-columns: 1fr 1fr; } .grid-2 { grid-template-columns: 1fr; } }
    /* Cards */
    .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 14px 16px; }
    .card-title { color: #8b8fa8; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
    .stat { font-size: 1.7rem; font-weight: 700; }
    /* Colors */
    .pos { color: #00c49a; } .neg { color: #ff4d6d; } .neu { color: #e0e0e0; }
    /* Section title */
    .sec { color: #8b8fa8; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
    /* Table */
    .tbl { width: 100%; border-collapse: collapse; }
    .tbl th { color: #8b8fa8; font-size: 0.72rem; text-transform: uppercase; padding: 6px 8px;
               border-bottom: 1px solid #2a2d3a; text-align: left; }
    .tbl td { padding: 7px 8px; border-bottom: 1px solid #1e2130; font-size: 0.85rem; vertical-align: middle; }
    .tbl tr:hover td { background: #22253a; }
    .mq { max-width: 240px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }
    .mono { font-family: monospace; font-size: 0.8rem; color: #8b8fa8; }
    /* Pills */
    .pill { padding: 2px 9px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; }
    .p-open { background: #0d2040; color: #4d9fff; }
    .p-win  { background: #0d3025; color: #00c49a; }
    .p-loss { background: #3d0d1a; color: #ff4d6d; }
    /* Log */
    .logbox { background: #0a0c12; border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px;
              height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.75rem;
              color: #a0a8b8; white-space: pre-wrap; word-break: break-all; }
    .le { color: #ff4d6d; } .lw { color: #ffd700; } .li { color: #a0a8b8; }
    /* Empty state */
    .empty { text-align: center; color: #555; padding: 20px; }
    /* Scroll container */
    .scroll { max-height: 280px; overflow-y: auto; }
    .mb { margin-bottom: 16px; }
    /* Config panel */
    .cfg-toggle { cursor: pointer; user-select: none; display: flex; align-items: center; gap: 8px; }
    .cfg-toggle:hover { color: #fff; }
    .cfg-toggle .arrow { font-size: 0.7rem; transition: transform 0.2s; }
    .cfg-toggle.open .arrow { transform: rotate(90deg); }
    .cfg-body { display: none; margin-top: 14px; }
    .cfg-body.open { display: block; }
    .cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    @media(max-width:700px) { .cfg-grid { grid-template-columns: 1fr; } }
    .cfg-field label { display: block; font-size: 0.78rem; font-weight: 600; color: #c0c4d8; margin-bottom: 3px; }
    .cfg-field .hint { font-size: 0.7rem; color: #666; margin-bottom: 5px; line-height: 1.4; }
    .cfg-field input, .cfg-field select {
      width: 100%; background: #0f1117; border: 1px solid #2a2d3a; border-radius: 6px;
      color: #e0e0e0; padding: 6px 10px; font-size: 0.85rem; }
    .cfg-field input:focus, .cfg-field select:focus { outline: none; border-color: #4d9fff; }
    .cfg-actions { margin-top: 16px; display: flex; align-items: center; gap: 12px; }
    .btn-save { background: #1a56db; color: #fff; border: none; border-radius: 6px;
                padding: 8px 20px; font-size: 0.85rem; font-weight: 600; cursor: pointer; }
    .btn-save:hover { background: #1e63ff; }
    .btn-save:disabled { background: #333; color: #666; cursor: not-allowed; }
    .cfg-status { font-size: 0.78rem; }
    .cfg-warn { color: #ffd700; font-size: 0.72rem; margin-top: 10px; padding: 8px 10px;
                background: #2a2500; border-radius: 6px; border-left: 3px solid #ffd700; }
    /* Period toggle */
    .period-toggle { display: flex; gap: 4px; }
    .period-btn { background: #13151f; border: 1px solid #2a2d3a; color: #8b8fa8;
                  border-radius: 6px; padding: 4px 14px; font-size: 0.75rem; font-weight: 600;
                  cursor: pointer; transition: background 0.15s, color 0.15s; }
    .period-btn:hover { background: #22253a; color: #e0e0e0; }
    .period-btn.active { background: #1a56db; border-color: #1a56db; color: #fff; }
  </style>
</head>
<body>

<div class="nav">
  <span class="nav-brand">Polymarket Copy-Bot</span>
  <span id="modeBadge" class="badge badge-paper">PAPER TRADE</span>
  <a href="/docs" style="font-size:0.82rem;color:#4d9fff;margin-left:8px">Docs</a>
  <span class="nav-right">Auto-refresh 30s &nbsp; <span id="lastRefresh">—</span></span>
</div>

<div class="page">

  <div class="grid-4">
    <div class="card"><div class="card-title">Starting Balance</div><div class="stat neu" id="startingBal">—</div></div>
    <div class="card"><div class="card-title">Portfolio Value</div><div class="stat neu" id="portfolioVal">—</div></div>
    <div class="card"><div class="card-title">Total P&amp;L</div><div class="stat neu" id="unrealizedPnl">—</div></div>
    <div class="card"><div class="card-title">Win Rate</div><div class="stat neu" id="winRate">—</div></div>
  </div>

  <div class="grid-4 mb">
    <div class="card"><div class="card-title">Open Positions</div><div class="stat neu" id="openTrades">—</div></div>
    <div class="card"><div class="card-title">Closed Trades</div><div class="stat neu" id="closedTrades">—</div></div>
    <div class="card"><div class="card-title">Capital Deployed</div><div class="stat neu" id="deployed">—</div></div>
    <div class="card"><div class="card-title">Whales Tracked</div><div class="stat neu" id="whaleCount">—</div></div>
  </div>

  <!-- Position Intelligence -->
  <div class="card mb" id="insightCard">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <span class="sec" style="margin-bottom:0">Position Intelligence</span>
      <span id="insightSlots" style="font-size:0.8rem;color:#8b8fa8"></span>
    </div>

    <!-- Open positions table -->
    <div id="insightPosWrap" style="overflow-x:auto">
      <table class="tbl" id="insightPosTable">
        <thead><tr>
          <th>Market</th><th>Outcome</th><th>Entry</th><th>Size</th><th>Cur Value</th><th>P&amp;L</th><th>Held for</th><th style="white-space:nowrap">Whale Signals</th>
        </tr></thead>
        <tbody id="insightPosBody"></tbody>
      </table>
    </div>
    <div id="insightPosEmpty" class="empty" style="display:none">No open positions right now.</div>

    <!-- Activity breakdown -->
    <div style="margin-top:16px">
      <div style="font-size:0.72rem;color:#8b8fa8;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">
        Whale Activity — Last 3 Hours
      </div>
      <div style="overflow-x:auto">
        <table class="tbl" id="insightActTable">
          <thead><tr>
            <th>Market</th><th>Signals</th><th>Whale Volume</th><th>Whales</th><th>Your Position</th>
          </tr></thead>
          <tbody id="insightActBody"></tbody>
        </table>
      </div>
      <div id="insightActEmpty" class="empty" style="display:none">No signals in the last 3 hours.</div>
    </div>

    <!-- Smart insight message -->
    <div id="insightMsg" style="display:none;margin-top:12px;padding:10px 14px;background:#1e2130;border-radius:6px;font-size:0.82rem;color:#c0c4d8;line-height:1.7"></div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <span class="sec" style="margin-bottom:0">Account Balance</span>
        <div class="period-toggle">
          <button class="period-btn active" id="btn-hours" onclick="setPeriod('hours')">Hours</button>
          <button class="period-btn" id="btn-days" onclick="setPeriod('days')">Days</button>
          <button class="period-btn" id="btn-weeks" onclick="setPeriod('weeks')">Weeks</button>
        </div>
      </div>
      <canvas id="balanceChart" height="160"></canvas>
      <div id="noBalanceData" class="empty" style="display:none">No closed trades yet — chart will appear once trades resolve.</div>
    </div>
    <div class="card scroll">
      <div class="sec">Recent Whale Activity</div>
      <table class="tbl"><thead><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Size</th></tr></thead>
      <tbody id="whaleBody"><tr><td colspan="4" class="empty">No activity yet</td></tr></tbody></table>
    </div>
  </div>

  <!-- Bot Settings -->
  <div class="card mb" id="cfgCard">
    <div class="sec cfg-toggle" id="cfgToggle">
      <span class="arrow">&#9654;</span> Bot Settings
      <span style="font-size:0.68rem;color:#444;font-weight:400;text-transform:none;letter-spacing:0">— click to expand</span>
    </div>
    <div class="cfg-body" id="cfgBody">
      <div class="cfg-grid">

        <div class="cfg-field">
          <label>Trading Mode</label>
          <div class="hint">Paper mode simulates trades without real money. Switch to Live only after thorough testing.</div>
          <select id="cfg_PAPER_TRADE">
            <option value="true">Paper Trade (Simulation)</option>
            <option value="false">Live Trade (Real Money)</option>
          </select>
        </div>

        <div class="cfg-field">
          <label>Poll Interval (seconds)</label>
          <div class="hint">How often the bot scans whale wallets for new trades. Lower = faster detection but more API calls. Recommended: 30–120.</div>
          <input type="number" id="cfg_POLL_INTERVAL_SECONDS" min="10" max="3600" step="10">
        </div>

        <div class="cfg-field">
          <label>Starting Balance (USDC)</label>
          <div class="hint">Your initial bankroll used for position sizing and the balance graph baseline. Set this to your Polymarket Cash balance when you started the bot.</div>
          <input type="number" id="cfg_LIVE_BANKROLL" min="0" step="1">
        </div>

        <div class="cfg-field">
          <label>Min Whale Trade Size (USDC)</label>
          <div class="hint">Only copy trades where the whale bet at least this amount. Higher = fewer but more confident signals. Use 5–20 for paper trading, 100+ for live.</div>
          <input type="number" id="cfg_MIN_WHALE_TRADE_USDC" min="1" step="1">
        </div>

        <div class="cfg-field">
          <label>Max Trade Size per Copy (USDC)</label>
          <div class="hint">Hard cap on how much USDC the bot spends per copied trade, regardless of bankroll size. Acts as a safety ceiling.</div>
          <input type="number" id="cfg_MAX_TRADE_USDC" min="1" step="1">
        </div>

        <div class="cfg-field">
          <label>Max Position Size (% of Bankroll)</label>
          <div class="hint">Maximum fraction of your total bankroll to risk on any single trade. 5% means a $1000 bankroll risks at most $50 per trade.</div>
          <input type="number" id="cfg_MAX_POSITION_FRACTION" min="0.1" max="100" step="0.1">
          <div class="hint" style="margin-top:4px">Entered as a percentage — e.g. enter 5 for 5%.</div>
        </div>

        <div class="cfg-field">
          <label>Max Open Positions</label>
          <div class="hint">Maximum number of simultaneous open trades. Prevents over-concentration. The bot skips new signals once this limit is reached.</div>
          <input type="number" id="cfg_MAX_OPEN_POSITIONS" min="1" max="100" step="1">
        </div>

        <div class="cfg-field">
          <label>Max Bid/Ask Spread (%)</label>
          <div class="hint">Skip markets where the spread between best bid and best ask exceeds this threshold. Wide spreads indicate low liquidity and higher slippage risk. Only applies in live mode.</div>
          <input type="number" id="cfg_MAX_SPREAD_PCT" min="0.1" max="100" step="0.1">
          <div class="hint" style="margin-top:4px">Entered as a percentage — e.g. enter 10 for 10%.</div>
        </div>

        <div class="cfg-field">
          <label>Stop-Loss (%)</label>
          <div class="hint">Close a position early if the current price drops this far below your entry price. Limits downside on bad trades. e.g. 50 means exit if price falls 50% from what you paid.</div>
          <input type="number" id="cfg_STOP_LOSS_PCT" min="1" max="99" step="1">
          <div class="hint" style="margin-top:4px">Entered as a percentage — e.g. enter 50 for 50%.</div>
        </div>

        <div class="cfg-field">
          <label>Take-Profit (%)</label>
          <div class="hint">Close a position early if the current price rises this far above your entry price. Locks in gains before a market reverses. e.g. 80 means exit if price rises 80% from entry.</div>
          <input type="number" id="cfg_TAKE_PROFIT_PCT" min="1" max="999" step="1">
          <div class="hint" style="margin-top:4px">Entered as a percentage — e.g. enter 80 for 80%.</div>
        </div>

      </div>

      <div id="liveWarning" class="cfg-warn" style="display:none">
        &#9888; You are switching to <strong>Live Trading</strong>. Real USDC will be spent.
        Make sure your wallet credentials are set in the .env file before saving.
      </div>

      <div class="cfg-actions">
        <button class="btn-save" id="cfgSave" onclick="saveConfig()">Save &amp; Restart Bot</button>
        <span class="cfg-status" id="cfgStatus"></span>
      </div>
    </div>
  </div>

  <!-- Bot log -->
  <div class="card mb">
    <div class="sec">Bot Log <span style="font-size:0.68rem;color:#444">(last 100 lines)</span></div>
    <div class="logbox" id="logBox">Loading...</div>
  </div>

  <div class="card">
    <div class="sec">Trade History</div>
    <div style="overflow-x:auto">
      <table class="tbl">
        <thead><tr><th>Date</th><th>Market</th><th>Outcome</th><th>Entry</th><th>Exit</th><th>Size</th><th>P&amp;L</th><th>Status</th></tr></thead>
        <tbody id="tradeBody"></tbody>
      </table>
      <div id="noTrades" class="empty">No trades logged yet. The bot will populate this as it detects whale activity.</div>
    </div>
  </div>

</div>

<script>
function fmt(n, d) {
  d = d === undefined ? 2 : d;
  if (n === null || n === undefined) return '—';
  return parseFloat(n).toFixed(d);
}
function fmtUsd(n) {
  if (n === null || n === undefined) return '—';
  var v = parseFloat(n);
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
}
function col(n) {
  if (n === null || n === undefined) return 'neu';
  return parseFloat(n) >= 0 ? 'pos' : 'neg';
}
function shortDate(s) {
  if (!s) return '—';
  return s.replace('T',' ').substring(0,16);
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Balance history chart ─────────────────────────────────────────────────────

var balanceChart = null;
var currentPeriod = 'hours';

function setPeriod(period) {
  currentPeriod = period;
  ['hours', 'days', 'weeks'].forEach(function(p) {
    document.getElementById('btn-' + p).classList.toggle('active', p === period);
  });
  loadBalanceHistory();
}

function loadBalanceHistory() {
  fetch('/api/balance_history?period=' + currentPeriod)
    .then(function(r){ return r.json(); })
    .then(function(data) {
      var canvas = document.getElementById('balanceChart');
      var noData = document.getElementById('noBalanceData');

      // Check if all balances are the same (no trades yet)
      var allSame = data.every(function(d){ return d.balance === data[0].balance; });
      if (allSame && data.length > 0) {
        // Still show the chart — flat line at starting balance
      }

      canvas.style.display = '';
      noData.style.display = 'none';

      var labels = data.map(function(d){ return d.label; });
      var balances = data.map(function(d){ return d.balance; });

      var minBal = Math.min.apply(null, balances);
      var maxBal = Math.max.apply(null, balances);
      var pad = Math.max((maxBal - minBal) * 0.15, 5);

      if (balanceChart) {
        balanceChart.data.labels = labels;
        balanceChart.data.datasets[0].data = balances;
        balanceChart.options.scales.y.min = Math.floor(minBal - pad);
        balanceChart.options.scales.y.max = Math.ceil(maxBal + pad);
        balanceChart.update();
        return;
      }

      balanceChart = new Chart(canvas, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Balance (USDC)',
            data: balances,
            borderColor: '#4d9fff',
            backgroundColor: 'rgba(77,159,255,0.10)',
            borderWidth: 2,
            pointRadius: 2,
            pointHoverRadius: 5,
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true,
          animation: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(ctx) {
                  return ' $' + ctx.parsed.y.toFixed(2);
                }
              },
              backgroundColor: '#1a1d27',
              borderColor: '#2a2d3a',
              borderWidth: 1,
              titleColor: '#8b8fa8',
              bodyColor: '#e0e0e0',
            }
          },
          scales: {
            x: {
              ticks: { color: '#8b8fa8', font: { size: 10 }, maxRotation: 0,
                       autoSkip: true, maxTicksLimit: 8 },
              grid: { color: '#1e2130' },
              border: { color: '#2a2d3a' },
            },
            y: {
              min: Math.floor(minBal - pad),
              max: Math.ceil(maxBal + pad),
              ticks: { color: '#8b8fa8', font: { size: 10 },
                       callback: function(v){ return '$' + v.toFixed(0); } },
              grid: { color: '#1e2130' },
              border: { color: '#2a2d3a' },
            }
          }
        }
      });
    })
    .catch(function(e){ console.error('balance_history:', e); });
}

function loadSummary() {
  fetch('/api/summary').then(function(r){ return r.json(); }).then(function(d) {
    var paper = d.paper_mode;
    var badge = document.getElementById('modeBadge');
    badge.className = 'badge ' + (paper ? 'badge-paper' : 'badge-live');
    badge.textContent = paper ? 'PAPER TRADE' : 'LIVE TRADE';
    document.getElementById('startingBal').textContent = d.starting_balance != null ? '$' + fmt(d.starting_balance) : '—';
    var pv = document.getElementById('portfolioVal');
    pv.textContent = d.portfolio_value != null ? '$' + fmt(d.portfolio_value) : '—';
    pv.className = 'stat ' + col(d.portfolio_value != null ? d.portfolio_value - d.starting_balance : 0);
    var upnl = document.getElementById('unrealizedPnl');
    upnl.textContent = d.total_pnl != null ? fmtUsd(d.total_pnl) : '—';
    upnl.className = 'stat ' + col(d.total_pnl);
    document.getElementById('winRate').textContent = d.win_rate + '%';
    document.getElementById('openTrades').textContent = d.open_trades;
    document.getElementById('closedTrades').textContent = d.closed_trades;
    document.getElementById('deployed').textContent = '$' + fmt(d.deployed_usdc);
    document.getElementById('whaleCount').textContent = d.whale_count;
  }).catch(function(e){ console.error('summary:', e); });
}

function loadTrades() {
  fetch('/api/trades').then(function(r){ return r.json(); }).then(function(rows) {
    var tbody = document.getElementById('tradeBody');
    var noTrades = document.getElementById('noTrades');
    if (!rows.length) { tbody.innerHTML = ''; noTrades.style.display = ''; return; }
    noTrades.style.display = 'none';
    tbody.innerHTML = rows.map(function(r) {
      var pnl = r.pnl_usdc;
      var pnlHtml = pnl !== null ? '<span class="'+col(pnl)+'">'+fmtUsd(pnl)+'</span>' : '<span class="neu">—</span>';
      var pill = r.status === 'OPEN' ? '<span class="pill p-open">Open</span>'
               : (pnl >= 0 ? '<span class="pill p-win">Win</span>' : '<span class="pill p-loss">Loss</span>');
      return '<tr><td>'+shortDate(r.placed_at)+'</td>'
        +'<td><span class="mq" title="'+esc(r.market_question||r.market_id)+'">'+esc(r.market_question||r.market_id)+'</span></td>'
        +'<td>'+esc(r.outcome)+'</td>'
        +'<td>'+fmt(r.entry_price,3)+'</td>'
        +'<td>'+(r.exit_price !== null ? fmt(r.exit_price,3) : '—')+'</td>'
        +'<td>$'+fmt(r.size_usdc)+'</td>'
        +'<td>'+pnlHtml+'</td>'
        +'<td>'+pill+'</td></tr>';
    }).join('');
  }).catch(function(e){ console.error('trades:', e); });
}

function loadWhales() {
  fetch('/api/whales').then(function(r){ return r.json(); }).then(function(rows) {
    var tbody = document.getElementById('whaleBody');
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="4" class="empty">No activity yet</td></tr>'; return; }
    tbody.innerHTML = rows.map(function(r) {
      return '<tr>'
        +'<td><span class="mono">'+esc(r.wallet_short)+'</span></td>'
        +'<td><span class="mq" title="'+esc(r.market_question)+'">'+esc(r.market_question||'—')+'</span></td>'
        +'<td>'+esc(r.outcome)+'</td>'
        +'<td class="pos">$'+fmt(r.size_usdc)+'</td></tr>';
    }).join('');
  }).catch(function(e){ console.error('whales:', e); });
}

function timeAgo(s) {
  if (!s) return '—';
  var mins = (Date.now() - new Date(s)) / 60000;
  if (mins < 1)    return 'just now';
  if (mins < 60)   return Math.round(mins) + 'm ago';
  if (mins < 1440) return Math.round(mins / 60) + 'h ago';
  return Math.round(mins / 1440) + 'd ago';
}

function loadInsights() {
  fetch('/api/insights').then(function(r){ return r.json(); }).then(function(d) {
    var s = d.stats;

    // Slot indicator in header
    var slotColor = s.slots_free > 0 ? '#00c49a' : '#ffd700';
    document.getElementById('insightSlots').innerHTML =
      '<span style="color:'+slotColor+';font-weight:700">'+s.open_count+' / '+s.max_positions+'</span>'
      + ' positions open &nbsp;|&nbsp; '
      + (s.slots_free > 0
          ? '<span style="color:#00c49a">'+s.slots_free+' slot'+(s.slots_free>1?'s':'')+' free</span>'
          : '<span style="color:#ffd700">Full</span>');

    // Open positions table
    var posBody = document.getElementById('insightPosBody');
    if (!d.positions.length) {
      document.getElementById('insightPosWrap').style.display = 'none';
      document.getElementById('insightPosEmpty').style.display = '';
    } else {
      document.getElementById('insightPosWrap').style.display = '';
      document.getElementById('insightPosEmpty').style.display = 'none';
      posBody.innerHTML = d.positions.map(function(p) {
        var sigColor = p.signals_since_entry > 5 ? 'pos' : (p.signals_since_entry > 0 ? 'neu' : 'neg');
        var curVal = p.cur_value != null ? '$' + fmt(p.cur_value, 2) : '—';
        var pnlCell = '—';
        if (p.pnl_usdc != null) {
          var pnlClass = p.pnl_usdc >= 0 ? 'pos' : 'neg';
          pnlCell = '<span class="'+pnlClass+'" style="font-weight:600">'
            + fmtUsd(p.pnl_usdc)
            + (p.pnl_pct != null ? ' <span style="font-size:0.78rem;opacity:0.75">('+p.pnl_pct+'%)</span>' : '')
            + '</span>';
        }
        return '<tr>'
          + '<td><span class="mq" title="'+esc(p.market_question)+'">'+esc(p.market_question)+'</span></td>'
          + '<td>'+esc(p.outcome)+'</td>'
          + '<td>'+fmt(p.entry_price, 3)+'</td>'
          + '<td>$'+fmt(p.size_usdc, 2)+'</td>'
          + '<td>'+curVal+'</td>'
          + '<td>'+pnlCell+'</td>'
          + '<td class="mono">'+timeAgo(p.placed_at)+'</td>'
          + '<td class="'+sigColor+'" style="font-weight:600">'+p.signals_since_entry+'</td>'
          + '</tr>';
      }).join('');
    }

    // Activity breakdown
    var actBody = document.getElementById('insightActBody');
    if (!d.activity.length) {
      document.getElementById('insightActTable').style.display = 'none';
      document.getElementById('insightActEmpty').style.display = '';
    } else {
      document.getElementById('insightActTable').style.display = '';
      document.getElementById('insightActEmpty').style.display = 'none';
      actBody.innerHTML = d.activity.map(function(a) {
        var posPill = a.we_hold
          ? '<span class="pill p-open">Holding</span>'
          : (s.slots_free > 0
              ? '<span style="color:#666;font-size:0.78rem">No position</span>'
              : '<span style="color:#555;font-size:0.78rem">Slots full</span>');
        return '<tr>'
          + '<td><span class="mq" title="'+esc(a.market_question)+'">'+esc(a.market_question)+'</span></td>'
          + '<td class="pos" style="font-weight:600">'+a.signal_count+'</td>'
          + '<td>$'+fmt(a.total_whale_usdc, 0)+'</td>'
          + '<td>'+a.whale_count+'</td>'
          + '<td>'+posPill+'</td>'
          + '</tr>';
      }).join('');
    }

    // Smart insight message
    var msg = '';
    if (s.total_signals_3h === 0) {
      msg = '&#128274; No whale signals in the last 3 hours — normal during off-peak hours. The bot will act as soon as a watched wallet makes a move.';
    } else if (s.slots_free === 0) {
      msg = '&#9989; All ' + s.max_positions + ' slots are filled. The bot is monitoring every position for exit signals. New signals are being skipped until a position closes.';
    } else if (s.pct_held >= 75 && s.slots_free > 0) {
      var focusedOn = d.activity.length === 1 ? ('the "' + d.activity[0].market_question.substring(0, 50) + '" market') : (d.activity.length + ' markets');
      msg = '&#128270; ' + s.pct_held + '% of recent signals (' + s.signals_in_held_markets + '/' + s.total_signals_3h + ') are in markets you already hold — the watched whales are concentrated on ' + focusedOn + ' right now. New slots will fill once they trade in different markets.';
    } else if (s.slots_free > 0 && s.total_signals_3h > 0) {
      var newMkt = d.activity.filter(function(a){ return !a.we_hold; }).length;
      msg = '&#9889; ' + s.slots_free + ' slot' + (s.slots_free > 1 ? 's' : '') + ' available. '
        + newMkt + ' new market' + (newMkt !== 1 ? 's' : '') + ' active in the last 3 hours — the bot is ready to copy the next qualifying signal.';
    }

    var msgDiv = document.getElementById('insightMsg');
    if (msg) {
      msgDiv.style.display = '';
      msgDiv.innerHTML = msg;
    } else {
      msgDiv.style.display = 'none';
    }
  }).catch(function(e){ console.error('insights:', e); });
}

function loadLog() {
  fetch('/api/log').then(function(r){ return r.json(); }).then(function(d) {
    var box = document.getElementById('logBox');
    box.innerHTML = d.lines.map(function(line) {
      var cls = (line.indexOf('[ERROR]') >= 0 || line.indexOf('[CRITICAL]') >= 0) ? 'le'
              : line.indexOf('[WARNING]') >= 0 ? 'lw' : 'li';
      return '<span class="'+cls+'">'+esc(line)+'</span>';
    }).join('\\n');
    box.scrollTop = box.scrollHeight;
  }).catch(function(e){
    document.getElementById('logBox').textContent = 'Error: ' + e;
  });
}

// ── Config panel ──────────────────────────────────────────────────────────────

document.getElementById('cfgToggle').addEventListener('click', function() {
  this.classList.toggle('open');
  document.getElementById('cfgBody').classList.toggle('open');
});

function loadConfig() {
  fetch('/api/config').then(function(r){ return r.json(); }).then(function(d) {
    document.getElementById('cfg_PAPER_TRADE').value          = d.PAPER_TRADE || 'true';
    document.getElementById('cfg_POLL_INTERVAL_SECONDS').value = d.POLL_INTERVAL_SECONDS || '60';
    document.getElementById('cfg_LIVE_BANKROLL').value         = d.LIVE_BANKROLL || '0';
    document.getElementById('cfg_MIN_WHALE_TRADE_USDC').value  = d.MIN_WHALE_TRADE_USDC || '5.0';
    document.getElementById('cfg_MAX_TRADE_USDC').value        = d.MAX_TRADE_USDC || '50.0';
    // stored as fraction (0.05), display as percent (5)
    var frac = parseFloat(d.MAX_POSITION_FRACTION || '0.05');
    document.getElementById('cfg_MAX_POSITION_FRACTION').value = (frac * 100).toFixed(1);
    document.getElementById('cfg_MAX_OPEN_POSITIONS').value    = d.MAX_OPEN_POSITIONS || '10';
    // stored as fraction (0.10), display as percent (10)
    var spread = parseFloat(d.MAX_SPREAD_PCT || '0.10');
    document.getElementById('cfg_MAX_SPREAD_PCT').value        = (spread * 100).toFixed(1);
    // stored as fraction (0.50), display as percent (50)
    var sl = parseFloat(d.STOP_LOSS_PCT || '0.50');
    document.getElementById('cfg_STOP_LOSS_PCT').value         = (sl * 100).toFixed(0);
    var tp = parseFloat(d.TAKE_PROFIT_PCT || '0.80');
    document.getElementById('cfg_TAKE_PROFIT_PCT').value       = (tp * 100).toFixed(0);

    // Show warning if already in live mode
    document.getElementById('liveWarning').style.display =
      (d.PAPER_TRADE === 'false') ? '' : 'none';
  }).catch(function(e){ console.error('config load:', e); });
}

document.getElementById('cfg_PAPER_TRADE').addEventListener('change', function() {
  document.getElementById('liveWarning').style.display =
    (this.value === 'false') ? '' : 'none';
});

function saveConfig() {
  var btn = document.getElementById('cfgSave');
  var status = document.getElementById('cfgStatus');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  status.textContent = '';

  var payload = {
    PAPER_TRADE:           document.getElementById('cfg_PAPER_TRADE').value,
    POLL_INTERVAL_SECONDS: document.getElementById('cfg_POLL_INTERVAL_SECONDS').value,
    LIVE_BANKROLL:         document.getElementById('cfg_LIVE_BANKROLL').value,
    MIN_WHALE_TRADE_USDC:  document.getElementById('cfg_MIN_WHALE_TRADE_USDC').value,
    MAX_TRADE_USDC:        document.getElementById('cfg_MAX_TRADE_USDC').value,
    // convert percent back to fraction
    MAX_POSITION_FRACTION: (parseFloat(document.getElementById('cfg_MAX_POSITION_FRACTION').value) / 100).toFixed(4),
    MAX_OPEN_POSITIONS:    document.getElementById('cfg_MAX_OPEN_POSITIONS').value,
    MAX_SPREAD_PCT:        (parseFloat(document.getElementById('cfg_MAX_SPREAD_PCT').value) / 100).toFixed(4),
    STOP_LOSS_PCT:         (parseFloat(document.getElementById('cfg_STOP_LOSS_PCT').value) / 100).toFixed(4),
    TAKE_PROFIT_PCT:       (parseFloat(document.getElementById('cfg_TAKE_PROFIT_PCT').value) / 100).toFixed(4),
  };

  fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function(r){ return r.json(); }).then(function(d) {
    btn.disabled = false;
    btn.textContent = 'Save & Restart Bot';
    if (d.ok) {
      status.style.color = '#00c49a';
      status.textContent = 'Saved. Bot is restarting...';
      setTimeout(function(){ status.textContent = ''; }, 5000);
    } else {
      status.style.color = '#ff4d6d';
      status.textContent = 'Error saving config.';
    }
  }).catch(function(e) {
    btn.disabled = false;
    btn.textContent = 'Save & Restart Bot';
    status.style.color = '#ff4d6d';
    status.textContent = 'Request failed: ' + e;
  });
}

// ── Main refresh ──────────────────────────────────────────────────────────────

function refresh() {
  loadSummary();
  loadTrades();
  loadWhales();
  loadInsights();
  loadBalanceHistory();
  loadLog();
  document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
}

loadConfig();
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype='text/html')


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5001")
    print("For internet access: ngrok http 5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
