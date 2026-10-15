from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..core.fields import count_value, is_two_sided, price_dollars


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_spread_cents(market: Dict) -> float:
    yes_bid = price_dollars(market, "yes_bid")
    yes_ask = price_dollars(market, "yes_ask")
    if yes_bid is None or yes_ask is None:
        return -1
    return round((yes_ask - yes_bid) * 100, 2)


def is_supported_binary_market(market: Dict) -> bool:
    market_type = str(market.get("market_type", "binary")).lower()
    ticker = str(market.get("ticker", ""))

    # Reject non-binary markets (scalar, etc.)
    if market_type != "binary":
        return False

    # Reject MVE (multivariate event) markets.
    # The API's mve_filter=exclude SHOULD filter these, but KXMVE* markets
    # have been observed slipping through. Multiple layers of defense:
    #
    # 1. Hard ticker-prefix gate: all MVE market tickers start with "KXMVE"
    if ticker.upper().startswith("KXMVE"):
        return False

    # 2. API response fields: mve_collection_ticker / mve_selected_legs
    #    (x-omitempty in the spec means absent when empty, present when set)
    if market.get("mve_collection_ticker"):
        return False
    mve_legs = market.get("mve_selected_legs")
    if mve_legs is not None and len(mve_legs) > 0:
        return False

    # 3. MVE combos use strike_type="functional" — reject those too
    strike_type = str(market.get("strike_type", "")).lower()
    if strike_type == "functional":
        return False

    return True


def minutes_to_close(market: Dict) -> float:
    close_time = market.get("close_time")
    if not close_time:
        return float("inf")
    try:
        closes_at = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (closes_at - datetime.now(timezone.utc)).total_seconds() / 60


def mid_price(market: Dict) -> Optional[float]:
    yes_bid = price_dollars(market, "yes_bid")
    yes_ask = price_dollars(market, "yes_ask")
    if yes_bid is None or yes_ask is None:
        return None
    return (yes_bid + yes_ask) / 2


def select_top_markets(markets: List[Dict], selector_cfg: Dict) -> List[Tuple[str, float, float, float]]:
    top_n = int(selector_cfg.get("top_n", 8))
    return rank_markets(markets, selector_cfg)[:top_n]


def rank_markets(markets: List[Dict], selector_cfg: Dict) -> List[Tuple[str, float, float, float]]:
    """All eligible markets as (ticker, score, volume_24h, spread_cents), best first."""
    min_volume_24h = safe_float(selector_cfg.get("min_volume_24h", 100))
    min_spread_cents = safe_float(selector_cfg.get("min_spread_cents", 1))
    volume_weight = safe_float(selector_cfg.get("volume_weight", 0.5))
    spread_weight = safe_float(selector_cfg.get("spread_weight", 0.5))
    min_minutes_to_close = safe_float(selector_cfg.get("min_minutes_to_close", 10))
    # Markets live on exchange shards and orders are funded from that shard's balance.
    exchange_indexes = selector_cfg.get("exchange_indexes")
    allowed_exchanges = None if exchange_indexes is None else {int(index) for index in exchange_indexes}
    # Near 0 or 1 the spread earned is tiny relative to the payout risked
    # (selling YES at 3c risks 97c), so only quote mid-range prices.
    min_mid_price = safe_float(selector_cfg.get("min_mid_price", 0.10))
    max_mid_price = safe_float(selector_cfg.get("max_mid_price", 0.90))

    def collect_candidates(ignore_thresholds: bool) -> List[Dict]:
        collected = []
        for market in markets:
            if not is_supported_binary_market(market):
                continue

            ticker = market.get("ticker")
            if not ticker:
                continue

            # Never quote one-sided books or markets about to close; these are hard
            # filters, not relaxed by the ignore_thresholds fallback below.
            if not is_two_sided(market) or minutes_to_close(market) < min_minutes_to_close:
                continue
            if allowed_exchanges is not None and int(market.get("exchange_index", 0)) not in allowed_exchanges:
                continue
            if not min_mid_price <= mid_price(market) <= max_mid_price:
                continue

            volume_24h = count_value(market, "volume_24h", default=count_value(market, "volume"))
            spread_cents = compute_spread_cents(market)
            if spread_cents < 0:
                continue

            if not ignore_thresholds and (volume_24h < min_volume_24h or spread_cents < min_spread_cents):
                continue

            collected.append(
                {
                    "ticker": ticker,
                    "volume_24h": volume_24h,
                    "spread_cents": spread_cents,
                }
            )
        return collected

    candidates = collect_candidates(ignore_thresholds=False)
    if not candidates:
        candidates = collect_candidates(ignore_thresholds=True)
    if not candidates:
        return []

    volumes = [market["volume_24h"] for market in candidates]
    spreads = [market["spread_cents"] for market in candidates]
    min_volume, max_volume = min(volumes), max(volumes)
    min_spread, max_spread = min(spreads), max(spreads)

    def normalize(value: float, low: float, high: float) -> float:
        if high == low:
            return 1.0
        return (value - low) / (high - low)

    ranked = []
    for market in candidates:
        volume_norm = normalize(market["volume_24h"], min_volume, max_volume)
        spread_norm = 1.0 - normalize(market["spread_cents"], min_spread, max_spread)
        score = volume_weight * volume_norm + spread_weight * spread_norm
        ranked.append((market["ticker"], score, market["volume_24h"], market["spread_cents"]))

    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked


def choose_markets(
    ranked: List[Tuple[str, float, float, float]],
    selected_since: Dict[str, float],
    now: float,
    selector_cfg: Dict,
) -> List[str]:
    """Pick top_n tickers, keeping incumbents to avoid churning quotes every refresh.

    An incumbent (currently selected) ticker that is still eligible keeps its slot
    while it is within min_hold_seconds of being selected, or while it still ranks
    within the top (top_n + retention_buffer). Remaining slots go to the best
    new candidates.
    """
    top_n = int(selector_cfg.get("top_n", 8))
    min_hold_seconds = safe_float(selector_cfg.get("min_hold_seconds", 300))
    retention_rank = top_n + int(selector_cfg.get("retention_buffer", top_n))

    rank_of = {row[0]: index for index, row in enumerate(ranked)}
    kept = [
        ticker
        for ticker, since in sorted(selected_since.items(), key=lambda item: rank_of.get(item[0], len(ranked)))
        if ticker in rank_of and (now - since < min_hold_seconds or rank_of[ticker] < retention_rank)
    ][:top_n]

    chosen = list(kept)
    for ticker, _, _, _ in ranked:
        if len(chosen) >= top_n:
            break
        if ticker not in chosen:
            chosen.append(ticker)
    return chosen
