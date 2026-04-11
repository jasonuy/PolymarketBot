"""
Wallet discovery — automatically finds the top performing wallets on Polymarket.

Strategy:
  1. Scrape the public Polymarket leaderboard page for wallet addresses
     (already sorted by profit — top wallets appear first)
  2. Fall back to a curated seed list of historically strong wallets if scraping fails

Called at bot startup when WATCHED_WALLETS is empty in config.py.
"""

import re
import logging
import requests

logger = logging.getLogger(__name__)

# How many top wallets to auto-follow
AUTO_FOLLOW_COUNT = 10

LEADERBOARD_URL = "https://polymarket.com/leaderboard"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Curated fallback list — known historically profitable Polymarket wallets.
# Used only if the leaderboard page cannot be reached.
# Update periodically by visiting polymarket.com/leaderboard.
SEED_WALLETS = [
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",
    "0x019782cab5d844f02bafb71f512758be78579f3c",
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "0xbddf61af533ff524d27154e589d2d7a81510c684",
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",
    "0x93abbc022ce98d6f45d4444b594791cc4b7a9723",
    "0xdc876e6873772d38716fda7f2452a78d426d7ab6",
]


def _scrape_leaderboard(n: int) -> list[str]:
    """
    Fetch the Polymarket leaderboard page and extract wallet addresses.
    The page is already sorted by profit so the first N addresses are the best.
    """
    try:
        resp = requests.get(LEADERBOARD_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Could not fetch leaderboard page: %s", exc)
        return []

    raw = re.findall(r'0x[a-fA-F0-9]{40}', resp.text)

    # Deduplicate preserving order, normalise to lowercase
    seen = set()
    unique = []
    for addr in raw:
        lower = addr.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(lower)

    # The very first addresses on the page tend to be contract/system addresses.
    # Skip the first one which is usually a platform contract.
    wallets = unique[1:n + 1] if len(unique) > 1 else unique[:n]
    return wallets


def discover_top_wallets(n: int = AUTO_FOLLOW_COUNT) -> list[str]:
    """
    Returns the top N wallet addresses to follow.
    Tries live leaderboard scraping first, falls back to seed list.
    """
    logger.info("Auto-discovering top wallets from Polymarket leaderboard...")

    wallets = _scrape_leaderboard(n)

    if wallets:
        logger.info("Discovered %d wallets from leaderboard:", len(wallets))
        for i, w in enumerate(wallets, 1):
            logger.info("  %d. %s", i, w)
        return wallets

    # Fallback to seed list
    logger.warning("Leaderboard scrape failed — using curated seed list")
    seed = SEED_WALLETS[:n]
    logger.info("Using %d seed wallets:", len(seed))
    for i, w in enumerate(seed, 1):
        logger.info("  %d. %s", i, w)
    return seed
