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

from flask import Flask, jsonify, Response
import sqlite3
from config import DB_PATH, PAPER_TRADE, MAX_TRADE_USDC

LOG_PATH = "bot.log"
LOG_TAIL_LINES = 100

app = Flask(__name__)

STARTING_BANKROLL = MAX_TRADE_USDC * 20  # same synthetic bankroll as bot.py


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
    """)
    closed = row.get("closed_trades", 0) or 0
    wins   = row.get("wins", 0) or 0
    row["win_rate"]         = round(wins / closed * 100, 1) if closed > 0 else 0
    row["starting_balance"] = STARTING_BANKROLL
    row["current_balance"]  = round(STARTING_BANKROLL + (row.get("total_pnl") or 0), 2)
    row["paper_mode"]       = PAPER_TRADE
    row["whale_count"]      = (query_one("SELECT COUNT(*) AS n FROM whale_trades") or {}).get("n", 0)
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


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy-Bot Dashboard</title>
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
  </style>
</head>
<body>

<div class="nav">
  <span class="nav-brand">Polymarket Copy-Bot</span>
  <span id="modeBadge" class="badge badge-paper">PAPER TRADE</span>
  <span class="nav-right">Auto-refresh 30s &nbsp; <span id="lastRefresh">—</span></span>
</div>

<div class="page">

  <div class="grid-4">
    <div class="card"><div class="card-title">Starting Balance</div><div class="stat neu" id="startBal">—</div></div>
    <div class="card"><div class="card-title">Current Balance</div><div class="stat neu" id="currBal">—</div></div>
    <div class="card"><div class="card-title">Total P&amp;L</div><div class="stat neu" id="totalPnl">—</div></div>
    <div class="card"><div class="card-title">Win Rate</div><div class="stat neu" id="winRate">—</div></div>
  </div>

  <div class="grid-4 mb">
    <div class="card"><div class="card-title">Open Positions</div><div class="stat neu" id="openTrades">—</div></div>
    <div class="card"><div class="card-title">Closed Trades</div><div class="stat neu" id="closedTrades">—</div></div>
    <div class="card"><div class="card-title">Capital Deployed</div><div class="stat neu" id="deployed">—</div></div>
    <div class="card"><div class="card-title">Whales Tracked</div><div class="stat neu" id="whaleCount">—</div></div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="sec">Cumulative P&amp;L</div>
      <div id="noChartData" class="empty">No closed trades yet — chart will appear once trades resolve.</div>
    </div>
    <div class="card scroll">
      <div class="sec">Recent Whale Activity</div>
      <table class="tbl"><thead><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Size</th></tr></thead>
      <tbody id="whaleBody"><tr><td colspan="4" class="empty">No activity yet</td></tr></tbody></table>
    </div>
  </div>

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

function loadSummary() {
  fetch('/api/summary').then(function(r){ return r.json(); }).then(function(d) {
    var paper = d.paper_mode;
    var badge = document.getElementById('modeBadge');
    badge.className = 'badge ' + (paper ? 'badge-paper' : 'badge-live');
    badge.textContent = paper ? 'PAPER TRADE' : 'LIVE TRADE';
    document.getElementById('startBal').textContent = '$' + fmt(d.starting_balance);
    var cb = document.getElementById('currBal');
    cb.textContent = '$' + fmt(d.current_balance);
    cb.className = 'stat ' + col(d.current_balance - d.starting_balance);
    var pnl = document.getElementById('totalPnl');
    pnl.textContent = fmtUsd(d.total_pnl);
    pnl.className = 'stat ' + col(d.total_pnl);
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

function refresh() {
  loadSummary();
  loadTrades();
  loadWhales();
  loadLog();
  document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
}

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
