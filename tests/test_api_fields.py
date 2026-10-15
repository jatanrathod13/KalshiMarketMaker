"""Tests for parsing the fixed-point Kalshi API payloads (V2 orders, *_dollars, *_fp)."""

from datetime import datetime, timedelta, timezone

from kalshi_market_maker.core.avellaneda import AvellanedaMarketMaker
from kalshi_market_maker.core.fields import is_two_sided, order_action, position_count, price_dollars
from kalshi_market_maker.selection.scoring import compute_spread_cents, select_top_markets


def market(ticker="M", bid="0.4500", ask="0.5500", bid_size="10.00", ask_size="10.00", volume="1000.00", close_in_min=120):
    close_time = (datetime.now(timezone.utc) + timedelta(minutes=close_in_min)).isoformat().replace("+00:00", "Z")
    return {
        "ticker": ticker,
        "market_type": "binary",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "yes_bid_size_fp": bid_size,
        "yes_ask_size_fp": ask_size,
        "volume_24h_fp": volume,
        "close_time": close_time,
    }


def test_price_reads_dollars_and_legacy_cents():
    assert price_dollars({"yes_bid_dollars": "0.4500"}, "yes_bid") == 0.45
    assert price_dollars({"yes_bid": 45}, "yes_bid") == 0.45
    assert price_dollars({}, "yes_bid") is None


def test_position_reads_fp_and_legacy():
    assert position_count({"position_fp": "-3.00"}) == -3
    assert position_count({"position": 2}) == 2


def test_empty_side_is_not_two_sided():
    assert is_two_sided(market())
    # The API reports an empty ask as $1.00 with zero size.
    assert not is_two_sided(market(bid="0.0100", ask="1.0000", ask_size="0.00"))
    assert not is_two_sided(market(bid="0.0000", bid_size="0.00"))


def test_order_action_uses_book_side():
    assert order_action({"book_side": "bid"}, "yes") == "buy"
    assert order_action({"book_side": "ask"}, "yes") == "sell"
    assert order_action({"book_side": "bid"}, "no") == "sell"
    assert order_action({"book_side": "ask"}, "no") == "buy"
    assert order_action({"side": "yes", "action": "buy"}, "yes") == "buy"


def test_spread_in_cents():
    assert compute_spread_cents(market()) == 10


def test_selector_skips_one_sided_and_closing_markets():
    cfg = {"min_volume_24h": 0, "min_spread_cents": 0, "top_n": 10, "min_minutes_to_close": 10}
    markets = [
        market("GOOD"),
        market("ONE_SIDED", bid="0.0100", ask="1.0000", ask_size="0.00"),
        market("CLOSING", close_in_min=5),
    ]
    assert [row[0] for row in select_top_markets(markets, cfg)] == ["GOOD"]


class OneSidedAPI:
    def __init__(self):
        self.canceled = []
        self.placed = []

    def get_price(self):
        return {"yes": None, "no": None}

    def get_position(self):
        raise AssertionError("should not fetch position without a two-sided book")

    def get_orders(self, ticker=None, status="resting"):
        return [{"order_id": "o1", "book_side": "bid"}]

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return True

    def place_order(self, *args, **kwargs):
        self.placed.append(args)


class NullLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def test_market_maker_pulls_quotes_when_book_one_sided():
    api = OneSidedAPI()
    mm = AvellanedaMarketMaker(logger=NullLogger(), api=api, gamma=0.2, k=150.0, sigma=0.1, T=0.05, max_position=3, order_expiration=60)
    mm.run(dt=0.1)
    assert api.canceled == ["o1"]
    assert api.placed == []
