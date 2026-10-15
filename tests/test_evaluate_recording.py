import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

import evaluate_recording as ev  # noqa: E402


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def synthetic_records():
    open_ts = 1_800_000_000 - (1_800_000_000 % 900)
    close_ts = open_ts + 900
    recs = []
    # 3 hours of flat-ish candles for vol warm-up, then 1 s spot during the window.
    rows = [[open_ts - 60 * i, 0, 0, 0, 100_000 + (i % 3) * 30, 1] for i in range(1, 200)]
    recs.append({"t": open_ts - 10, "kind": "candles", "product": "BTC-USD", "rows": rows})
    recs.append({"t": open_ts, "kind": "market", "product": "BTC-USD",
                 "market": {"ticker": "KXBTC15M-X", "open_time": iso(open_ts), "close_time": iso(close_ts),
                            "floor_strike": 100_000}})
    for s in range(0, 840, 2):
        t = open_ts + s
        recs.append({"t": t, "kind": "spot", "product": "BTC-USD", "price": str(100_000 + s / 4)})
        recs.append({"t": t, "kind": "book", "ticker": "KXBTC15M-X",
                     "book": {"orderbook_fp": {"yes_dollars": [["0.4000", "50"]], "no_dollars": [["0.4000", "50"]]}}})
        if s % 30 == 0:
            recs.append({"t": t + 1, "kind": "trades", "ticker": "KXBTC15M-X", "trades": [
                {"trade_id": f"a{s}", "yes_price_dollars": "0.3000", "count_fp": "5", "taker_side": "no",
                 "created_time": iso(t + 1)},
                {"trade_id": f"b{s}", "yes_price_dollars": "0.7000", "count_fp": "5", "taker_side": "yes",
                 "created_time": iso(t + 1)}]})
    recs.append({"t": close_ts + 70, "kind": "result", "ticker": "KXBTC15M-X", "result": "yes", "market": {}})
    return recs


def test_parse_book_formats():
    new = {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.52", "3"]]}}
    old = {"orderbook": {"yes": [[45, 10]], "no": [[52, 3]]}}
    assert ev.parse_book(new) == ev.parse_book(old) == ([(0.45, 10.0)], [(0.52, 3.0)])


def test_fees():
    assert ev.taker_fee(0.5) == 0.018  # 0.07*10*0.25 = 17.5c → 18c per 10 lots
    assert ev.maker_fee(0.5) == 0.005  # 4.375c → 5c per 10 lots


def test_evaluate_runs_end_to_end(tmp_path):
    path = tmp_path / "rec.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in synthetic_records()))
    gap, brier, taker, maker = ev.evaluate(ev.load([path]))
    assert gap and brier
    # Spot drifts up so FV rises above the static 40/60 book: buying YES at 0.60 should trigger.
    assert taker[0.0]
    # Prints at 0.30 / 0.70 go through both our bids: YES fills win, NO fills lose (settles YES).
    fills = maker[(0.02, "through")]
    assert any(p > 0 for p in fills) and any(p < 0 for p in fills)
    assert len(maker[(0.02, "touch")]) >= len(fills)
