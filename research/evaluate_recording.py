"""Replay a recording from recorder.py and answer the questions that decide
whether there's an edge:

  A. How far is the Kalshi mid from our calibrated fair value, by minute?
  B. Who forecasts better (Brier score): the Kalshi mid or our fair value?
  C. Taker test: buy whenever the ask is below FV by more than fee + margin.
  D. Maker test: rest bids at FV - h. Fills only from real prints, in two cases:
     strictly through our price (pessimistic) and also at our price (optimistic).
     Each qualifying print counts as one filled contract; size and queue limits
     are not modelled, so read D as P&L per fill, not as achievable volume.

    python research/evaluate_recording.py data/recordings/*.jsonl
"""
import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kalshi_market_maker.model import EwmaVol, FairValueModel  # noqa: E402

LOT = 10  # fees are rounded up per order; assume 10-contract orders


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def taker_fee(p: float) -> float:
    return math.ceil(0.07 * LOT * p * (1 - p) * 100 - 1e-9) / 100 / LOT


def maker_fee(p: float) -> float:
    return math.ceil(0.0175 * LOT * p * (1 - p) * 100 - 1e-9) / 100 / LOT


def _levels(raw, scale):
    out = []
    for level in raw or []:
        price, size = float(level[0]) / scale, float(level[1])
        if size > 0:
            out.append((round(price, 4), size))
    return out


def parse_book(payload: dict):
    """Return (yes_bids, no_bids) as [(price_dollars, size)]; handles old and new formats."""
    book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    if book.get("yes_dollars") is not None or book.get("no_dollars") is not None:
        return _levels(book.get("yes_dollars"), 1), _levels(book.get("no_dollars"), 1)
    return _levels(book.get("yes"), 100), _levels(book.get("no"), 100)


def trade_fields(t: dict):
    price = t.get("yes_price_dollars")
    price = float(price) if price is not None else float(t["yes_price"]) / 100
    count = float(t.get("count_fp") or t.get("count") or 0)
    return ts(t["created_time"]), round(price, 4), count, t.get("taker_side")


def load(paths):
    records = []
    for p in paths:
        with open(p) as f:
            records.extend(json.loads(line) for line in f if line.strip())
    records.sort(key=lambda r: r["t"])
    return records


class Product:
    """Spot path and EWMA vol for one Coinbase product."""

    def __init__(self):
        self.minute_close = {}
        self.spot_t, self.spot_p = [], []

    def add_candles(self, rows):
        for row in rows:  # [time, low, high, open, close, volume]
            self.minute_close[int(row[0]) // 60 + 1] = float(row[4])  # close is known at the end of the minute

    def add_spot(self, t, price):
        self.spot_t.append(t)
        self.spot_p.append(price)
        self.minute_close[int(t) // 60 + 1] = price

    def spot_at(self, t):
        i = bisect_right(self.spot_t, t) - 1
        return self.spot_p[i] if i >= 0 and t - self.spot_t[i] < 5 else None

    def build_vol(self):
        vol, self.sigma_by_minute = EwmaVol(), {}
        for minute in sorted(self.minute_close):
            vol.update(self.minute_close[minute])
            self.sigma_by_minute[minute] = vol.sigma
        self.vol_minutes = sorted(self.sigma_by_minute)

    def sigma_at(self, t):
        i = bisect_right(self.vol_minutes, int(t) // 60) - 1
        return self.sigma_by_minute[self.vol_minutes[i]] if i >= 120 else None  # need ~2 h of warm-up

    def avg_before(self, t):
        lo, hi = bisect_right(self.spot_t, t - 60), bisect_right(self.spot_t, t)
        window = self.spot_p[lo:hi]
        return sum(window) / len(window) if window else None


def evaluate(records, margins=(0.0, 0.01, 0.02, 0.03), half_spreads=(0.01, 0.02, 0.03, 0.04)):
    model = FairValueModel()
    products = defaultdict(Product)
    markets, results, books = {}, {}, defaultdict(list)
    trades = defaultdict(list)
    for r in records:
        kind = r["kind"]
        if kind == "candles":
            products[r["product"]].add_candles(r["rows"])
        elif kind == "spot" and r.get("price") is not None:
            products[r["product"]].add_spot(r["t"], float(r["price"]))
        elif kind == "market":
            markets[r["market"]["ticker"]] = (r["product"], r["market"])
        elif kind == "book":
            books[r["ticker"]].append((r["t"], *parse_book(r["book"])))
        elif kind == "trades":
            trades[r["ticker"]].extend(trade_fields(t) for t in r["trades"])
        elif kind == "result":
            results[r["ticker"]] = 1 if r["result"] == "yes" else 0
    for p in products.values():
        p.build_vol()

    gap = defaultdict(list)
    brier = defaultdict(lambda: [0.0, 0.0, 0])
    taker = {m: [] for m in margins}
    maker = {(h, mode): [] for h in half_spreads for mode in ("through", "touch")}

    for ticker, snaps in books.items():
        if ticker not in markets:
            continue
        product_name, market = markets[ticker]
        prod = products[product_name]
        open_ts, close_ts = ts(market["open_time"]), ts(market["close_time"])
        strike = market.get("floor_strike") or prod.avg_before(open_ts)
        if not strike:
            continue
        strike = float(strike)
        outcome = results.get(ticker)
        tr = sorted(trades.get(ticker, []))
        tr_t = [x[0] for x in tr]
        last_taker_minute = {}
        brier_done = set()
        snap_t = [s[0] for s in snaps]
        for t, yes_bids, no_bids in snaps:
            elapsed = (t - open_ts) / 60
            spot, sigma = prod.spot_at(t), prod.sigma_at(t)
            if not 0 <= elapsed <= 14 or spot is None or sigma is None or not yes_bids or not no_bids:
                continue
            fv = model.p_yes(spot, strike, sigma, elapsed)
            bid = max(p for p, _ in yes_bids)
            ask = 1 - max(p for p, _ in no_bids)
            mid = (bid + ask) / 2
            k = int(elapsed)
            gap[k].append((mid - fv, ask - bid))
            if outcome is not None and k in (1, 3, 6, 9, 12) and k not in brier_done:
                brier_done.add(k)
                b = brier[k]
                b[0] += (mid - outcome) ** 2
                b[1] += (fv - outcome) ** 2
                b[2] += 1
            if outcome is None or elapsed > 12:
                continue
            # C. Taker, at most one entry per side per minute per ticker.
            for mgn in margins:
                for side, price, fair in (("yes", ask, fv), ("no", 1 - bid, 1 - fv)):
                    key = (mgn, side, k)
                    if key in last_taker_minute or price >= 1 or price <= 0:
                        continue
                    if price + taker_fee(price) + mgn < fair:
                        last_taker_minute[key] = True
                        win = outcome if side == "yes" else 1 - outcome
                        taker[mgn].append(win - price - taker_fee(price))
            # D. Maker: our resting bids until the next snapshot.
            nxt = bisect_right(snap_t, t)
            t_end = snaps[nxt][0] if nxt < len(snaps) else t + 2
            window = tr[bisect_right(tr_t, t + 0.25): bisect_right(tr_t, t_end)]  # 250 ms placement latency
            for h in half_spreads:
                our_yes = math.floor((fv - h) * 100) / 100
                our_no = math.floor((1 - fv - h) * 100) / 100
                our_yes = min(our_yes, round(ask - 0.01, 2))
                our_no = min(our_no, round(1 - bid - 0.01, 2))
                for _, price, _, taker_side in window:
                    if our_yes > 0 and taker_side == "no":
                        if price <= our_yes:
                            pnl = outcome - our_yes - maker_fee(our_yes)
                            maker[(h, "touch")].append(pnl)
                            if price < our_yes:
                                maker[(h, "through")].append(pnl)
                    if our_no > 0 and taker_side == "yes":
                        no_price = round(1 - price, 4)
                        if no_price <= our_no:
                            pnl = (1 - outcome) - our_no - maker_fee(our_no)
                            maker[(h, "touch")].append(pnl)
                            if no_price < our_no:
                                maker[(h, "through")].append(pnl)
    return gap, brier, taker, maker


def summarize(values):
    n = len(values)
    if n == 0:
        return "n=0"
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / max(n - 1, 1))
    return f"n={n:6d} mean={100 * mean:+.2f}c  ±{196 * sd / math.sqrt(n):.2f}c (95%, ignores clustering)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args()
    gap, brier, taker, maker = evaluate(load(args.paths))
    print("A. Kalshi mid − fair value, by minute elapsed")
    for k in sorted(gap):
        g = [x[0] for x in gap[k]]
        s = [x[1] for x in gap[k]]
        print(f"  min {k:2d}: n={len(g):6d} mean={100 * sum(g) / len(g):+.2f}c  "
              f"mean |gap|={100 * sum(abs(x) for x in g) / len(g):.2f}c  avg spread={100 * sum(s) / len(s):.2f}c")
    print("B. Brier score (lower is better): Kalshi mid vs our FV")
    for k in sorted(brier):
        km, fv, n = brier[k]
        if n:
            print(f"  min {k:2d}: n={n:5d} kalshi={km / n:.4f} ours={fv / n:.4f}")
    print("C. Taker: buy when ask + fee + margin < FV  (P&L per contract, held to settlement)")
    for m, v in taker.items():
        print(f"  margin {100 * m:.0f}c: {summarize(v)}")
    print("D. Maker: bids at FV − h (P&L per filled contract, held to settlement)")
    for (h, mode), v in sorted(maker.items()):
        print(f"  h={100 * h:.0f}c {mode:7s}: {summarize(v)}")


if __name__ == "__main__":
    main()
