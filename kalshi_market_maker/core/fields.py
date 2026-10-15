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
