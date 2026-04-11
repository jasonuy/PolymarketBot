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

from flask import Flask, jsonify, render_template_string
import sqlite3
from config import DB_PATH, PAPER_TRADE, MAX_TRADE_USDC

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

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy-Bot Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
    .navbar { background: #1a1d27 !important; border-bottom: 1px solid #2a2d3a; }
    .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 12px; }
    .card-title { color: #8b8fa8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; }
    .stat-value { font-size: 1.8rem; font-weight: 700; }
    .positive { color: #00c49a; }
    .negative { color: #ff4d6d; }
    .neutral  { color: #e0e0e0; }
    .badge-paper { background: #3d3a00; color: #ffd700; }
    .badge-live  { background: #003d1a; color: #00ff88; }
    .table { color: #e0e0e0; }
    .table thead th { color: #8b8fa8; border-color: #2a2d3a; font-size: 0.78rem; text-transform: uppercase; }
    .table td { border-color: #2a2d3a; vertical-align: middle; font-size: 0.88rem; }
    .table tbody tr:hover { background: #22253a; }
    .status-open   { color: #4d9fff; }
    .status-closed { color: #8b8fa8; }
    .pill { padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
    .pill-open   { background: #0d2040; color: #4d9fff; }
    .pill-closed { background: #1a1d27; color: #8b8fa8; border: 1px solid #2a2d3a; }
    .pill-win    { background: #0d3025; color: #00c49a; }
    .pill-loss   { background: #3d0d1a; color: #ff4d6d; }
    .section-title { color: #8b8fa8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
    #pnlChart { max-height: 220px; }
    .refresh-badge { font-size: 0.72rem; color: #555; }
    .whale-wallet { font-family: monospace; font-size: 0.82rem; color: #8b8fa8; }
    .market-q { max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  </style>
</head>
<body>

<nav class="navbar navbar-dark px-4 py-3">
  <span class="navbar-brand fw-bold">📈 Polymarket Copy-Bot</span>
  <span id="modeBadge" class="pill badge-paper">PAPER TRADE</span>
  <span class="refresh-badge ms-auto">Auto-refresh every 30s &nbsp; <span id="lastRefresh">—</span></span>
</nav>

<div class="container-fluid px-4 py-4">

  <!-- Summary cards -->
  <div class="row g-3 mb-4" id="summaryCards">
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Starting Balance</div>
      <div class="stat-value neutral" id="startBal">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Current Balance</div>
      <div class="stat-value" id="currBal">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Total P&L</div>
      <div class="stat-value" id="totalPnl">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Win Rate</div>
      <div class="stat-value neutral" id="winRate">—</div>
    </div></div>
  </div>

  <!-- Second row -->
  <div class="row g-3 mb-4">
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Open Positions</div>
      <div class="stat-value neutral" id="openTrades">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Closed Trades</div>
      <div class="stat-value neutral" id="closedTrades">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Capital Deployed</div>
      <div class="stat-value neutral" id="deployed">—</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card p-3">
      <div class="card-title">Whales Tracked</div>
      <div class="stat-value neutral" id="whaleCount">—</div>
    </div></div>
  </div>

  <!-- P&L chart + whale activity -->
  <div class="row g-3 mb-4">
    <div class="col-md-7">
      <div class="card p-3">
        <div class="section-title">Cumulative P&L</div>
        <canvas id="pnlChart"></canvas>
        <div id="noChartData" class="text-center text-muted py-4" style="display:none">
          No closed trades yet — P&L chart will appear once trades resolve.
        </div>
      </div>
    </div>
    <div class="col-md-5">
      <div class="card p-3" style="max-height:320px; overflow-y:auto;">
        <div class="section-title">Recent Whale Activity</div>
        <table class="table table-sm mb-0">
          <thead><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Size</th></tr></thead>
          <tbody id="whaleBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Trade history -->
  <div class="card p-3">
    <div class="section-title">Trade History</div>
    <div class="table-responsive">
      <table class="table table-sm mb-0">
        <thead>
          <tr>
            <th>Date</th><th>Market</th><th>Outcome</th>
            <th>Entry</th><th>Exit</th><th>Size</th><th>P&L</th><th>Status</th>
          </tr>
        </thead>
        <tbody id="tradeBody"></tbody>
      </table>
      <div id="noTrades" class="text-center text-muted py-4" style="display:none">
        No trades logged yet. The bot will populate this table as it detects whale activity.
      </div>
    </div>
  </div>

</div>

<script>
let pnlChart = null;

function fmt(n, decimals=2) {
  if (n === null || n === undefined) return '—';
  return parseFloat(n).toFixed(decimals);
}
function fmtUsd(n) {
  if (n === null || n === undefined) return '—';
  const v = parseFloat(n);
  return (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
}
function colorClass(n) {
  if (n === null || n === undefined) return 'neutral';
  return parseFloat(n) >= 0 ? 'positive' : 'negative';
}
function shortDate(s) {
  if (!s) return '—';
  return s.replace('T', ' ').substring(0, 16);
}

async function loadSummary() {
  const d = await fetch('/api/summary').then(r => r.json());

  document.getElementById('modeBadge').className = 'pill ' + (d.paper_mode ? 'badge-paper' : 'badge-live');
  document.getElementById('modeBadge').textContent = d.paper_mode ? 'PAPER TRADE' : 'LIVE TRADE';

  document.getElementById('startBal').textContent = '$' + fmt(d.starting_balance);

  const currEl = document.getElementById('currBal');
  currEl.textContent = '$' + fmt(d.current_balance);
  currEl.className = 'stat-value ' + colorClass(d.current_balance - d.starting_balance);

  const pnlEl = document.getElementById('totalPnl');
  pnlEl.textContent = fmtUsd(d.total_pnl);
  pnlEl.className = 'stat-value ' + colorClass(d.total_pnl);

  document.getElementById('winRate').textContent = d.win_rate + '%';
  document.getElementById('openTrades').textContent = d.open_trades;
  document.getElementById('closedTrades').textContent = d.closed_trades;
  document.getElementById('deployed').textContent = '$' + fmt(d.deployed_usdc);
  document.getElementById('whaleCount').textContent = d.whale_count;
}

async function loadTrades() {
  const rows = await fetch('/api/trades').then(r => r.json());
  const tbody = document.getElementById('tradeBody');
  const noTrades = document.getElementById('noTrades');

  if (!rows.length) { tbody.innerHTML = ''; noTrades.style.display = ''; return; }
  noTrades.style.display = 'none';

  tbody.innerHTML = rows.map(r => {
    const pnl = r.pnl_usdc;
    const pnlStr = pnl !== null ? `<span class="${colorClass(pnl)}">${fmtUsd(pnl)}</span>` : '<span class="neutral">—</span>';
    const statusPill = r.status === 'OPEN'
      ? '<span class="pill pill-open">Open</span>'
      : (pnl >= 0 ? '<span class="pill pill-win">Win</span>' : '<span class="pill pill-loss">Loss</span>');
    return `<tr>
      <td>${shortDate(r.placed_at)}</td>
      <td><div class="market-q" title="${r.market_question || r.market_id}">${r.market_question || r.market_id}</div></td>
      <td>${r.outcome}</td>
      <td>${fmt(r.entry_price, 3)}</td>
      <td>${r.exit_price !== null ? fmt(r.exit_price, 3) : '—'}</td>
      <td>$${fmt(r.size_usdc)}</td>
      <td>${pnlStr}</td>
      <td>${statusPill}</td>
    </tr>`;
  }).join('');
}

async function loadWhales() {
  const rows = await fetch('/api/whales').then(r => r.json());
  const tbody = document.getElementById('whaleBody');
  if (!rows.length) { tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">No activity yet</td></tr>'; return; }
  tbody.innerHTML = rows.map(r => `<tr>
    <td><span class="whale-wallet">${r.wallet_short}</span></td>
    <td><div class="market-q" title="${r.market_question}">${r.market_question || '—'}</div></td>
    <td>${r.outcome}</td>
    <td class="positive">$${fmt(r.size_usdc)}</td>
  </tr>`).join('');
}

async function loadChart() {
  const rows = await fetch('/api/pnl_over_time').then(r => r.json());
  const canvas = document.getElementById('pnlChart');
  const noData = document.getElementById('noChartData');

  if (!rows.length) { canvas.style.display = 'none'; noData.style.display = ''; return; }
  canvas.style.display = '';
  noData.style.display = 'none';

  const labels = rows.map(r => shortDate(r.date));
  const data   = rows.map(r => r.cumulative_pnl);
  const lastVal = data[data.length - 1] || 0;
  const color  = lastVal >= 0 ? '#00c49a' : '#ff4d6d';

  if (pnlChart) { pnlChart.destroy(); }
  pnlChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Cumulative P&L (USDC)',
        data,
        borderColor: color,
        backgroundColor: color + '22',
        borderWidth: 2,
        pointRadius: 3,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#555', maxTicksLimit: 6 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#555' }, grid: { color: '#2a2d3a' } }
      }
    }
  });
}

async function refresh() {
  await Promise.all([loadSummary(), loadTrades(), loadWhales(), loadChart()]);
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
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5001")
    print("For internet access: ngrok http 5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
