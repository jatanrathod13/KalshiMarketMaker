"""Tests for market eligibility, ranking and scoring in selection.scoring."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_market_maker.selection.scoring import (
    is_supported_binary_market,
    mid_price,
    minutes_to_close,
    rank_markets,
    safe_float,
    select_top_markets,
)


def market(ticker="M", bid="0.4500", ask="0.5500", volume="1000.00", close_in_min=120, **extra):
    close_time = (datetime.now(timezone.utc) + timedelta(minutes=close_in_min)).isoformat().replace("+00:00", "Z")
    data = {
        "ticker": ticker,
        "market_type": "binary",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "yes_bid_size_fp": "10.00",
        "yes_ask_size_fp": "10.00",
        "volume_24h_fp": volume,
        "close_time": close_time,
    }
    data.update(extra)
    return data


def tickers(rows):
    return [row[0] for row in rows]


def test_safe_float_falls_back_on_garbage():
    assert safe_float("1.5") == 1.5
    assert safe_float(None, 7) == 7
    assert safe_float("abc", 2.0) == 2.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"market_type": "scalar"},
        {"ticker": "KXMVEFOO-1"},
        {"ticker": "kxmvefoo-1"},
        {"mve_collection_ticker": "COLL"},
        {"mve_selected_legs": [{"ticker": "A"}]},
        {"strike_type": "functional"},
    ],
)
def test_unsupported_markets_rejected(overrides):
    assert not is_supported_binary_market(market(**overrides))


def test_plain_binary_market_supported():
    assert is_supported_binary_market(market())
    assert is_supported_binary_market(market(mve_selected_legs=[]))


def test_minutes_to_close_handles_missing_and_bad_values():
    assert minutes_to_close({}) == float("inf")
    assert minutes_to_close({"close_time": "not-a-date"}) == float("inf")
    assert 59 < minutes_to_close(market(close_in_min=60)) <= 60


def test_mid_price():
    assert mid_price(market(bid="0.40", ask="0.60")) == pytest.approx(0.5)
    assert mid_price({"ticker": "X"}) is None


def test_rank_excludes_unsupported_and_tickerless_markets():
    rows = rank_markets([market("OK"), market("KXMVE-1"), market(ticker=""), market("S", market_type="scalar")], {})
    assert tickers(rows) == ["OK"]


def test_rank_returns_empty_for_no_markets():
    assert rank_markets([], {}) == []


def test_rank_prefers_higher_volume_and_tighter_spread():
    markets = [
        market("LOWVOL", volume="200"),
        market("HIGHVOL", volume="5000"),
        market("WIDE", volume="200", bid="0.40", ask="0.60"),
    ]
    rows = rank_markets(markets, {"min_volume_24h": 100})
    assert tickers(rows)[0] == "HIGHVOL"
    assert tickers(rows)[-1] == "WIDE"


def test_rank_row_shape_and_spread_cents():
    (row,) = rank_markets([market("A", bid="0.45", ask="0.55", volume="500")], {})
    assert row[0] == "A"
    assert row[2] == 500
    assert row[3] == 10.0


def test_weights_change_ordering():
    markets = [
        market("VOLUME", volume="9000", bid="0.40", ask="0.60"),
        market("TIGHT", volume="200", bid="0.49", ask="0.51"),
    ]
    assert tickers(rank_markets(markets, {"volume_weight": 1, "spread_weight": 0}))[0] == "VOLUME"
    assert tickers(rank_markets(markets, {"volume_weight": 0, "spread_weight": 1}))[0] == "TIGHT"


def test_thresholds_filter_when_some_markets_qualify():
    markets = [market("BIG", volume="1000"), market("SMALL", volume="5")]
    assert tickers(rank_markets(markets, {"min_volume_24h": 100})) == ["BIG"]


def test_min_spread_threshold():
    markets = [market("WIDE", bid="0.45", ask="0.55"), market("TIGHT", bid="0.499", ask="0.501")]
    assert tickers(rank_markets(markets, {"min_spread_cents": 5})) == ["WIDE"]


def test_thresholds_relaxed_when_nothing_qualifies():
    rows = rank_markets([market("SMALL", volume="5")], {"min_volume_24h": 100})
    assert tickers(rows) == ["SMALL"]


def test_hard_filters_not_relaxed_by_fallback():
    closing = market("CLOSING", close_in_min=2, volume="5")
    one_sided = market("ONESIDED", volume="5", yes_bid_size_fp="0.00")
    assert rank_markets([closing, one_sided], {"min_volume_24h": 100}) == []


def test_min_minutes_to_close_configurable():
    m = market("SOON", close_in_min=30)
    assert tickers(rank_markets([m], {"min_minutes_to_close": 10})) == ["SOON"]
    assert rank_markets([m], {"min_minutes_to_close": 60}) == []


def test_mid_price_band_configurable():
    m = market("LOW", bid="0.14", ask="0.16")
    assert tickers(rank_markets([m], {})) == ["LOW"]
    assert rank_markets([m], {"min_mid_price": 0.2}) == []
    assert rank_markets([market("HI", bid="0.84", ask="0.86")], {"max_mid_price": 0.8}) == []


def test_exchange_index_filter_defaults_to_shard_zero_when_absent():
    markets = [market("A"), market("B", exchange_index=1)]
    assert tickers(rank_markets(markets, {"exchange_indexes": [1]})) == ["B"]
    assert set(tickers(rank_markets(markets, {"exchange_indexes": [0, 1]}))) == {"A", "B"}
    assert set(tickers(rank_markets(markets, {}))) == {"A", "B"}


def test_identical_markets_score_equally_and_survive():
    rows = rank_markets([market("A"), market("B")], {})
    assert sorted(tickers(rows)) == ["A", "B"]
    assert len({row[1] for row in rows}) == 1


def test_select_top_markets_limits_to_top_n():
    markets = [market(f"M{i}", volume=str(1000 + i)) for i in range(5)]
    rows = select_top_markets(markets, {"top_n": 3})
    assert len(rows) == 3
    assert tickers(rows)[0] == "M4"
