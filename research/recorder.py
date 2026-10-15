"""Record Kalshi 15-minute crypto markets alongside Coinbase spot, for replay.

Public, unauthenticated endpoints only. Nothing here places orders.

    python research/recorder.py --out data/recordings --series KXBTC15M:BTC-USD KXETH15M:ETH-USD

Writes one JSONL file per UTC day, with these record types:
  candles  Coinbase 1-minute candles (warm-up for the volatility estimate)
  spot     Coinbase ticker (price, bid, ask)
  market   Kalshi market metadata (strike, open/close times), once per ticker
  book     Kalshi order book (YES bids and NO bids, every level)
  trades   new Kalshi prints since the last poll
  result   settlement for a closed ticker
Leave it running for at least 3-7 days, then run research/evaluate_recording.py.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"


def get(url, params=None, retries=4):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(0.5 * 2 ** attempt)


class Writer:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, payload: dict):
        now = time.time()
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        with open(self.out_dir / f"{day}.jsonl", "a") as f:
            f.write(json.dumps({"t": now, "kind": kind, **payload}) + "\n")


class SeriesRecorder:
    def __init__(self, series: str, product: str, writer: Writer):
        self.series, self.product, self.w = series, product, writer
        self.ticker = None
        self.close_ts = 0.0
        self.last_trade_ts = int(time.time())
        self.seen_trade_ids = set()
        self.pending_results = {}  # ticker -> close_ts

    def warm_up(self):
        candles = get(f"{COINBASE}/products/{self.product}/candles", {"granularity": 60})
        self.w.write("candles", {"product": self.product, "rows": candles})

    def refresh_market(self):
        data = get(f"{KALSHI}/markets", {"series_ticker": self.series, "status": "open", "limit": 50})
        now = time.time()
        best = None
        for m in data.get("markets", []):
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
            if close > now and (best is None or close < best[0]):
                best = (close, m)
        if best and best[1]["ticker"] != self.ticker:
            if self.ticker:
                self.pending_results[self.ticker] = self.close_ts
            self.close_ts, market = best
            self.ticker = market["ticker"]
            self.w.write("market", {"series": self.series, "product": self.product, "market": market})

    def poll(self):
        if self.ticker is None or time.time() >= self.close_ts:
            self.refresh_market()
        if self.ticker is None:
            return
        spot = get(f"{COINBASE}/products/{self.product}/ticker")
        self.w.write("spot", {"product": self.product, "price": spot.get("price"), "bid": spot.get("bid"),
                              "ask": spot.get("ask"), "time": spot.get("time")})
        book = get(f"{KALSHI}/markets/{self.ticker}/orderbook")
        self.w.write("book", {"ticker": self.ticker, "book": book})
        trades = get(f"{KALSHI}/markets/trades", {"ticker": self.ticker, "min_ts": self.last_trade_ts - 5, "limit": 1000})
        fresh = [t for t in trades.get("trades", []) if t.get("trade_id") not in self.seen_trade_ids]
        if fresh:
            self.seen_trade_ids.update(t.get("trade_id") for t in fresh)
            self.w.write("trades", {"ticker": self.ticker, "trades": fresh})
        self.last_trade_ts = int(time.time())

    def collect_results(self):
        for ticker, close_ts in list(self.pending_results.items()):
            if time.time() < close_ts + 60:
                continue
            market = get(f"{KALSHI}/markets/{ticker}").get("market", {})
            if market.get("result") in ("yes", "no"):
                self.w.write("result", {"ticker": ticker, "result": market["result"], "market": market})
                del self.pending_results[ticker]
            elif time.time() > close_ts + 3600:
                del self.pending_results[ticker]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/recordings")
    ap.add_argument("--series", nargs="+", default=["KXBTC15M:BTC-USD", "KXETH15M:ETH-USD"])
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between polls per series")
    args = ap.parse_args()
    writer = Writer(Path(args.out))
    recorders = [SeriesRecorder(*s.split(":"), writer) for s in args.series]
    last_warm, last_results = 0.0, 0.0
    while True:
        started = time.time()
        if started - last_warm > 3600:
            for r in recorders:
                try:
                    r.warm_up()
                except requests.RequestException as e:
                    print("warm-up failed:", e)
            last_warm = started
        for r in recorders:
            try:
                r.poll()
            except (requests.RequestException, KeyError, ValueError) as e:
                print(f"{datetime.now(timezone.utc).isoformat()} {r.series} poll failed: {e}")
        if started - last_results > 30:
            for r in recorders:
                try:
                    r.collect_results()
                except requests.RequestException as e:
                    print("result fetch failed:", e)
            last_results = started
        time.sleep(max(0.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
