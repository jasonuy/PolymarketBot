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


def get_wallet_positions(wallet: str) -> list[dict]:
    """Returns current open positions for a wallet."""
    data = _get(f"{DATA_API}/positions", params={"user": wallet})
    if isinstance(data, list):
        return data
    return []


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
    Returns the current mid-price for a token (YES or NO side of a market).
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
        if best_ask and best_bid and best_ask > 0:
            return (best_ask - best_bid) / best_ask
    except (KeyError, IndexError, ValueError, TypeError):
        pass
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
