"""Readers for Kalshi API payload fields.

The API has moved from integer cents / integer counts (``yes_bid``, ``position``)
to fixed-point strings (``yes_bid_dollars``, ``position_fp``). These helpers read
the new fields and fall back to the legacy ones.
"""

from typing import Dict, Optional


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price_dollars(payload: Dict, base_key: str) -> Optional[float]:
    """Price in dollars from ``<base_key>_dollars`` or legacy cents ``<base_key>``."""
    dollars = _to_float(payload.get(f"{base_key}_dollars"))
    if dollars is not None:
        return dollars
    cents = _to_float(payload.get(base_key))
    return None if cents is None else cents / 100


def count_value(payload: Dict, base_key: str, default: float = 0.0) -> float:
    """Contract count from ``<base_key>_fp`` or legacy ``<base_key>``."""
    for key in (f"{base_key}_fp", base_key):
        value = _to_float(payload.get(key))
        if value is not None:
            return value
    return default


def position_count(position: Dict) -> int:
    return int(count_value(position, "position"))


def has_yes_bid(market: Dict) -> bool:
    if market.get("yes_bid_size_fp") is not None:
        return count_value(market, "yes_bid_size") > 0
    bid = price_dollars(market, "yes_bid")
    return bid is not None and bid > 0


def has_yes_ask(market: Dict) -> bool:
    if market.get("yes_ask_size_fp") is not None:
        return count_value(market, "yes_ask_size") > 0
    ask = price_dollars(market, "yes_ask")
    return ask is not None and ask < 1


def is_two_sided(market: Dict) -> bool:
    return has_yes_bid(market) and has_yes_ask(market)


def order_action(order: Dict, trade_side: str) -> Optional[str]:
    """Buy/sell direction of an order relative to ``trade_side``.

    Uses ``book_side`` (bid = buy YES / sell NO); the legacy ``side``/``action``
    fields are deprecated by the API and only used as a fallback.
    """
    book_side = order.get("book_side")
    if book_side in ("bid", "ask"):
        buys_yes = book_side == "bid"
        return "buy" if buys_yes == (trade_side == "yes") else "sell"
    if order.get("side") == trade_side:
        return order.get("action")
    return None
