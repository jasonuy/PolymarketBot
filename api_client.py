"""
Thin wrapper around the Polymarket REST APIs.

Endpoints used:
  DATA API  – https://data-api.polymarket.com   (activity, positions)
  GAMMA API – https://gamma-api.polymarket.com  (market metadata, prices)
  CLOB API  – https://clob.polymarket.com        (order book, balance)
"""

import logging
import requests
from typing import Optional
from config import DATA_API, GAMMA_API, CLOB_HOST

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-copy-bot/1.0"})

# Token IDs that returned 404 — market order book is closed, skip price checks.
# Cleared on bot restart so resolution checks still run every cycle.
_dead_token_ids: set[str] = set()


def _get(url: str, params: dict = None) -> Optional[dict | list]:
    try:
        resp = SESSION.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            # 404 is expected for closed/delisted markets — log at DEBUG not ERROR
            logger.debug("GET %s → 404 (market likely closed)", url)
        else:
            logger.error("GET %s failed: %s", url, exc)
        return None
    except requests.RequestException as exc:
        logger.error("GET %s failed: %s", url, exc)
        return None


# ── Wallet activity ───────────────────────────────────────────────────────────

def get_wallet_activity(wallet: str, limit: int = 50) -> list[dict]:
    """
    Returns recent trade activity for a wallet address.
    Each item contains: market, outcome, side, price, size, timestamp, transactionHash
    """
    data = _get(f"{DATA_API}/activity", params={"user": wallet, "limit": limit})
    if isinstance(data, list):
        return data
    return []


def get_all_wallet_activity(wallet: str) -> list[dict]:
    """
    Returns the full trade history for a wallet, paginating until exhausted.
    """
    all_trades: list[dict] = []
    limit, offset = 100, 0
    while True:
        data = _get(f"{DATA_API}/activity", params={"user": wallet, "limit": limit, "offset": offset})
        if not isinstance(data, list) or not data:
            break
        all_trades.extend(data)
        if len(data) < limit:
            break
        offset += limit
    return all_trades


def get_starting_balance(wallet: str, current_cash: float) -> Optional[float]:
    """
    Derive net deposits by working backwards from current cash and trade history.

    Formula: net_deposits = current_cash + total_bought_usdc - total_sold_usdc

    Every dollar deposited either stayed as cash or was used to buy positions.
    Sell proceeds return to cash. So:
        deposits = cash + (buys - sells)

    Returns None if activity cannot be fetched.
    """
    trades = get_all_wallet_activity(wallet)
    if not trades:
        return None
    total_bought = sum(t.get("usdcSize", 0) for t in trades if t.get("side") == "BUY")
    total_sold   = sum(t.get("usdcSize", 0) for t in trades if t.get("side") == "SELL")
    return round(current_cash + total_bought - total_sold, 2)


def get_wallet_positions(wallet: str) -> list[dict]:
    """Returns current open positions for a wallet."""
    data = _get(f"{DATA_API}/positions", params={"user": wallet})
    if isinstance(data, list):
        return data
    return []


def get_portfolio(funder_address: str) -> dict:
    """
    Fetch live portfolio data from the Polymarket Data API.
    Returns cash (from CLOB), open position values, and totals — mirroring
    what Polymarket displays in their UI.

    Keys returned:
      positions_value  – sum of current market value of all open positions
      unrealized_pnl   – sum of cashPnl across all open positions
      positions        – list of raw position dicts from the Data API,
                         keyed by conditionId for easy DB enrichment
    """
    positions = get_wallet_positions(funder_address)
    positions_value = round(sum(p.get("currentValue", 0) for p in positions), 2)
    unrealized_pnl  = round(sum(p.get("cashPnl", 0)      for p in positions), 2)
    # Index by conditionId so callers can join with DB market_id
    by_condition = {p["conditionId"].lower(): p for p in positions if p.get("conditionId")}
    return {
        "positions_value": positions_value,
        "unrealized_pnl":  unrealized_pnl,
        "positions":       positions,
        "by_condition":    by_condition,
    }


# ── Market data ───────────────────────────────────────────────────────────────

def get_market(condition_id: str) -> Optional[dict]:
    """Fetch metadata for a single market by condition_id."""
    data = _get(f"{GAMMA_API}/markets", params={"conditionId": condition_id})
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        markets = data.get("markets")
        if isinstance(markets, list):
            return markets[0] if markets else None
        return data
    return None


def get_markets(active: bool = True, limit: int = 100) -> list[dict]:
    """Fetch a list of markets."""
    data = _get(f"{GAMMA_API}/markets", params={"active": str(active).lower(), "limit": limit})
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "markets" in data:
        return data["markets"]
    return []


def get_market_price(token_id: str) -> Optional[float]:
    """
    Returns the current best-ask price for a token (cost to BUY one share).
    token_id is the CLOB token id for that outcome.
    Returns None (silently) for tokens whose order book is known to be closed.
    """
    if token_id in _dead_token_ids:
        return None

    try:
        resp = SESSION.get(
            f"{CLOB_HOST}/price",
            params={"token_id": token_id, "side": "BUY"},
            timeout=10,
        )
        if resp.status_code == 404:
            _dead_token_ids.add(token_id)
            logger.debug("token_id %s...  → 404, suppressing future price checks", token_id[:12])
            return None
        resp.raise_for_status()
        data = resp.json()
        if data and "price" in data:
            return float(data["price"])
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.error("get_market_price(%s...) failed: %s", token_id[:12], exc)
    return None


def get_best_bid(token_id: str) -> Optional[float]:
    """
    Returns the current best-bid price for a token (proceeds from selling one share).
    This is the price to use when placing a SELL order for immediate fill.
    Returns None if the token is dead or the request fails.
    """
    if token_id in _dead_token_ids:
        return None
    try:
        resp = SESSION.get(
            f"{CLOB_HOST}/price",
            params={"token_id": token_id, "side": "SELL"},
            timeout=10,
        )
        if resp.status_code == 404:
            _dead_token_ids.add(token_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        if data and "price" in data:
            return float(data["price"])
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.error("get_best_bid(%s...) failed: %s", token_id[:12], exc)
    return None


def get_order_book(token_id: str) -> Optional[dict]:
    """Returns the order book for a token (bids and asks)."""
    return _get(f"{CLOB_HOST}/book", params={"token_id": token_id})


def get_spread(token_id: str) -> Optional[float]:
    """
    Returns the bid-ask spread as a fraction (0.0 – 1.0).
    Returns None if the order book is unavailable.
    """
    book = get_order_book(token_id)
    if not book:
        return None
    try:
        best_ask = float(book["asks"][0]["price"]) if book.get("asks") else None
        best_bid = float(book["bids"][0]["price"]) if book.get("bids") else None
        # Note: use explicit None checks — best_bid of 0.0 is valid and must not
        # be treated as falsy.
        if best_ask is not None and best_bid is not None and best_ask > 0:
            return (best_ask - best_bid) / best_ask
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def get_spread_from_gamma(condition_id: str, slug: str = "") -> Optional[float]:
    """
    Returns the market spread from GAMMA API metadata.

    Preferred over get_spread() for neg-risk / group markets (O/U, spreads,
    multi-outcome game markets) where the individual CLOB token order book is
    managed at the group level and the per-token book appears hollow.
    GAMMA's spread field already aggregates liquidity correctly.

    For neg-risk markets the conditionId from the activity API is a group ID
    that GAMMA may not resolve correctly. We try slug lookup first (most
    reliable), then fall back to conditionId, then to None.
    """
    def _extract_spread(market: dict) -> Optional[float]:
        if not market:
            return None
        try:
            spread = market.get("spread")
            if spread is not None:
                return float(spread)
            bid = market.get("bestBid")
            ask = market.get("bestAsk")
            if bid is not None and ask is not None:
                bid, ask = float(bid), float(ask)
                if ask > 0 and bid >= 0:
                    return (ask - bid) / ask
        except (TypeError, ValueError):
            pass
        return None

    # 1. Try slug — most reliable for neg-risk game markets
    if slug:
        data = _get(f"{GAMMA_API}/markets", params={"slug": slug})
        markets = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for m in markets:
            if m and m.get("slug") == slug:
                result = _extract_spread(m)
                if result is not None:
                    return result

    # 2. Try conditionId — works for standard (non-neg-risk) markets
    if condition_id:
        market = get_market(condition_id)
        # Verify GAMMA returned the correct market (not an unrelated one)
        if market and market.get("conditionId", "").lower() == condition_id.lower():
            result = _extract_spread(market)
            if result is not None:
                return result

    return None


def get_market_resolution(condition_id: str, outcome: str) -> Optional[float]:
    """
    Returns the resolution price for a specific outcome in a closed market.
    Returns 1.0 if the outcome won, 0.0 if it lost, None if unresolved/unknown.

    Uses the CLOB /markets/{condition_id} endpoint which returns a 'tokens' array
    with 'winner' booleans and 'closed' flag for resolved markets.

    outcome: the string outcome label, e.g. "Yes" / "No" / "Over" / "Under",
             or a numeric index string "0" / "1".
    """
    data = _get(f"{CLOB_HOST}/markets/{condition_id}")
    if not data or not isinstance(data, dict):
        return None

    # Only report resolution when the market is fully closed
    if not data.get("closed"):
        return None

    tokens = data.get("tokens") or []

    # Match by outcome label (case-insensitive)
    for token in tokens:
        if str(token.get("outcome", "")).strip().lower() == str(outcome).strip().lower():
            return 1.0 if token.get("winner") else 0.0

    # Fallback: treat outcome as a positional index (0 = first token, etc.)
    try:
        idx = int(outcome)
        if 0 <= idx < len(tokens):
            return 1.0 if tokens[idx].get("winner") else 0.0
    except (ValueError, TypeError):
        pass

    return None


# ── Account ───────────────────────────────────────────────────────────────────

def get_token_balance(wallet: str, token_id: str) -> Optional[float]:
    """
    Returns the actual number of shares held for a given token_id in the wallet.
    Uses the Polymarket positions API — this is the exact balance the CLOB will
    accept when placing a SELL order (avoids rounding mismatches from recalculating
    shares as size_usdc / entry_price).
    Returns None if the position is not found or the request fails.
    """
    positions = get_wallet_positions(wallet)
    for pos in positions:
        if str(pos.get("asset", "")).lower() == str(token_id).lower():
            try:
                return float(pos["size"])
            except (KeyError, TypeError, ValueError):
                pass
    return None


def get_usdc_balance(clob_client) -> float:
    """
    Returns the USDC balance available in the Polymarket CLOB account.
    Requires an authenticated py-clob-client instance.
    """
    try:
        balance = clob_client.get_balance()
        return float(balance)
    except Exception as exc:
        logger.error("Failed to fetch USDC balance: %s", exc)
        return 0.0
