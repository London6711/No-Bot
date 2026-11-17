"""
Polymarket "Buy NO" Bot — Improved Strategy
Paper trading is the default. Live mode requires explicit triple-flag opt-in.
"""

import os
import time
import json
import logging
import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BookParams
from py_clob_client.order_builder.constants import BUY

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("buy_no_bot")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Buy NO Bot")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run a single scan cycle in paper mode and exit (no live orders)",
    )
    return parser.parse_args()

# ── Configuration (non-secret, safe to commit) ─────────────────────────────
CONFIG = {
    # IMPROVEMENT 1: Wider NO-price band based on the favorite-longshot bias.
    # Original cap was 0.08. Research shows the profitable NO edge is in
    # buying cheap NO on strong favorites (YES 0.75–0.95 → NO 0.05–0.25),
    # especially in the final hours.  We scan 0.05–0.25 and let filters decide.
    "min_no_price": 0.05,
    "max_no_price": 0.25,

    # IMPROVEMENT 2: Time-window filter. The favorite-longshot mispricing
    # intensifies in the final 1–12 hours before resolution. Ignore markets
    # that are too far away or already past their end time.
    "min_hours_to_resolution": 0.5,    # ignore markets closing in <30 min
    "max_hours_to_resolution": 12.0,   # ignore markets >12 h away

    # IMPROVEMENT 3: Liquidity floor. Avoid illiquid markets where the
    # quoted NO price is stale or the order book is too thin to fill.
    "min_liquidity_usdc": 500.0,
    "min_volume_24h_usdc": 100.0,

    # IMPROVEMENT 4: Limit orders only (GTC). Market orders eat the spread.
    # We place a resting limit buy at our target price and wait for a fill.
    "use_limit_orders": True,
    "limit_price_offset": 0.005,       # place limit 0.5¢ below mid to be maker

    # Position sizing
    "starting_capital_usdc": 50.0,
    "order_size_usdc": 50.0,           # starting capital per order
    "max_open_positions": 20,
    "max_capital_per_market_pct": 0.05,  # never >5% of bankroll in one market

    # Category filters
    "exclude_keywords": [
        "sport", "nfl", "nba", "mlb", "nhl", "soccer", "tennis",
        "football", "basketball", "baseball", "hockey",
    ],

    # Loop timing
    "scan_interval_seconds": 300,      # 5 min between scans
}

# ── Secrets (environment variables only — never hardcode) ──────────────────
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS")

# Triple-flag live-mode safety (all three must be set correctly)
BOT_MODE = os.getenv("BOT_MODE", "paper")
DRY_RUN = os.getenv("DRY_RUN", "true")
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false")

def is_live_mode() -> bool:
    return (
        BOT_MODE == "live"
        and DRY_RUN.lower() == "false"
        and LIVE_TRADING_ENABLED.lower() == "true"
    )

# ── Polymarket Gamma API helpers (public, no auth) ─────────────────────────
GAMMA_BASE = "https://gamma-api.polymarket.com"

def fetch_active_markets(limit: int = 500) -> list[dict]:
    """Fetch active, non-closed binary markets from Gamma API."""
    params = {
        "closed": "false",
        "active": "true",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        r = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Gamma market fetch failed: %s", e)
        return []

def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

def hours_until(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 3600.0

def is_binary_yes_no(market: dict) -> bool:
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return False
    return (
        isinstance(outcomes, list)
        and len(outcomes) == 2
        and set(o.lower() for o in outcomes) == {"yes", "no"}
    )

def is_excluded_category(market: dict) -> bool:
    text = " ".join([
        market.get("question", ""),
        market.get("category", "") or "",
        market.get("groupSlug", "") or "",
    ]).lower()
    return any(kw in text for kw in CONFIG["exclude_keywords"])

def get_no_token_id(market: dict) -> Optional[str]:
    """Extract the CLOB token ID for the NO outcome."""
    try:
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        outcomes = market.get("outcomes")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if not clob_ids or not outcomes:
            return None
        for oid, olabel in zip(clob_ids, outcomes):
            if olabel.lower() == "no":
                return oid
    except Exception:
        pass
    return None

# ── CLOB client (read-only vs. authenticated) ─────────────────────────────
def build_client(authenticated: bool = False) -> ClobClient:
    if not authenticated:
        return ClobClient(HOST)
    if not PRIVATE_KEY:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is not set.")
    kwargs = {
        "key": PRIVATE_KEY,
        "chain_id": CHAIN_ID,
        "signature_type": 0 if not FUNDER else 1,
    }
    if FUNDER:
        kwargs["funder"] = FUNDER
    client = ClobClient(HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

# ── Order placement (limit vs. market) ────────────────────────────────────
def place_buy_no_order(
    client: ClobClient,
    token_id: str,
    size_usdc: float,
    limit_price: float,
) -> dict:
    """
    Place a GTC limit BUY for NO shares.

    py-clob-client treats `size` as the number of shares, not USDC.
    We convert: shares = USDC / limit_price.
    """
    shares = round(size_usdc / limit_price, 2)
    if shares < 5:  # Polymarket minimum order size is 5 shares
        log.warning("Order too small (%.2f shares < 5 minimum). Skipping.", shares)
        return {}

    order_args = OrderArgs(
        token_id=token_id,
        price=limit_price,
        size=shares,
        side=BUY,
    )
    signed = client.create_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    log.info(
        "Limit BUY NO | token=%s | shares=%.2f | limit=%.3f | resp=%s",
        token_id[:12], shares, limit_price, resp,
    )
    return resp

# ── Paper-trading simulator ────────────────────────────────────────────────
class PaperPortfolio:
    def __init__(self):
        self.positions: dict[str, dict] = {}

    def record_fill(self, token_id: str, shares: float, price: float):
        pos = self.positions.get(token_id, {"shares": 0.0, "cost": 0.0})
        pos["shares"] += shares
        pos["cost"] += shares * price
        self.positions[token_id] = pos
        log.info(
            "[PAPER] Filled NO | token=%s | shares=%.2f @ %.3f | total cost=$%.2f",
            token_id[:12], shares, price, pos["cost"],
        )

    def open_count(self) -> int:
        return len(self.positions)

# ── Main strategy loop ─────────────────────────────────────────────────────
def run_once(client: Optional[ClobClient], paper: PaperPortfolio):
    markets = fetch_active_markets()
    log.info("Fetched %d active markets.", len(markets))

    # Enforce max order size as a pct of starting capital (5% cap).
    max_order_size = CONFIG["starting_capital_usdc"] * CONFIG["max_capital_per_market_pct"]
    order_size_usdc = min(CONFIG["order_size_usdc"], max_order_size)

    candidates = 0
    for m in markets:
        if not is_binary_yes_no(m):
            continue
        if is_excluded_category(m):
            continue

        # Liquidity & volume filters
        liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        volume_24h = float(m.get("volume24hr") or 0)
        if liquidity < CONFIG["min_liquidity_usdc"]:
            continue
        if volume_24h < CONFIG["min_volume_24h_usdc"]:
            continue

        # Time-window filter
        end_dt = parse_iso(m.get("endDate"))
        hrs = hours_until(end_dt)
        if hrs is None or hrs < CONFIG["min_hours_to_resolution"]:
            continue
        if hrs > CONFIG["max_hours_to_resolution"]:
            continue

        # NO token & price
        token_id = get_no_token_id(m)
        if not token_id:
            continue

        # Read current NO price from the order book
        try:
            book = client.get_order_book(BookParams(token_id=token_id))
            asks = getattr(book, "asks", None) or []
            bids = getattr(book, "bids", None) or []
            if not asks and not bids:
                continue
            best_ask = min(float(a.price) for a in asks) if asks else None
            best_bid = max(float(b.price) for b in bids) if bids else None
        except Exception as e:
            log.debug("Order book fetch failed for %s: %s", token_id[:12], e)
            continue

        # Determine NO price (prefer best ask for a buyer; fallback to mid)
        if best_ask is not None:
            no_price = best_ask
        elif best_bid is not None:
            no_price = best_bid
        else:
            continue

        if not (CONFIG["min_no_price"] <= no_price <= CONFIG["max_no_price"]):
            continue

        # Position-limit checks
        if paper.open_count() >= CONFIG["max_open_positions"]:
            log.info("Max open positions reached. Stopping scan.")
            break

        candidates += 1
        log.info(
            "CANDIDATE | %s | NO=%.3f | liquidity=$%.0f | vol24h=$%.0f | hrs_left=%.1f",
            m.get("question", "")[:80], no_price, liquidity, volume_24h, hrs,
        )

        # Limit price: sit slightly below the current ask to be a maker
        limit_price = max(0.01, round(no_price - CONFIG["limit_price_offset"], 3))

        if is_live_mode() and client is not None:
            try:
                place_buy_no_order(
                    client=client,
                    token_id=token_id,
                    size_usdc=order_size_usdc,
                    limit_price=limit_price,
                )
            except Exception as e:
                log.error("Live order failed for %s: %s", token_id[:12], e)
        else:
            shares = round(order_size_usdc / limit_price, 2)
            paper.record_fill(token_id, shares, limit_price)

    log.info("Scan complete. %d candidates acted on.", candidates)

# ── Entry point ────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    live = is_live_mode()

    if args.self_test:
        log.info("Running self-test (single paper scan)...")
        paper = PaperPortfolio()
        client = build_client(authenticated=False)
        run_once(client, paper)
        log.info("Self-test complete.")
        return

    log.info("Starting Buy-NO bot | LIVE=%s", live)

    paper = PaperPortfolio()
    client = build_client(authenticated=live) if live else build_client(authenticated=False)

    while True:
        try:
            run_once(client, paper)
        except KeyboardInterrupt:
            log.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            log.exception("Unhandled error in main loop: %s", e)

        log.info("Sleeping %ds until next scan...", CONFIG["scan_interval_seconds"])
        time.sleep(CONFIG["scan_interval_seconds"])

if __name__ == "__main__":
    main()