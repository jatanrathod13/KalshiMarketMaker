"""Build KXBTC15M-style 15-minute windows from 1-minute OHLC data.

Settlement proxy: Kalshi compares the 60 s average of the CF BRTI before the
window close with the 60 s average before the window open. We approximate each
60 s average with the typical price (O+H+L+C)/4 of the 1-minute candle that
ends at the boundary.
"""
import sys

import numpy as np
import pandas as pd


def load_minutes(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("ts")
    full = pd.date_range(df.index[0], df.index[-1], freq="1min")
    df = df.reindex(full)
    df["close"] = df["close"].ffill()
    for col in ("open", "high", "low"):
        df[col] = df[col].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    df["typ"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    return df


def build_windows(m: pd.DataFrame) -> pd.DataFrame:
    """One row per 15-minute window [T, T+15m), with settlement proxy and path."""
    # Candle starting at T-1m ends at T: its typical price is the "avg before T".
    ref = m["typ"].shift(1)  # value indexed at T = avg of minute [T-1m, T)
    starts = m.index[(m.index.minute % 15 == 0)]
    starts = starts[(starts >= m.index[0] + pd.Timedelta("6h")) & (starts + pd.Timedelta("15min") <= m.index[-1])]
    w = pd.DataFrame(index=starts)
    w["ref_open"] = ref.reindex(starts).values
    w["ref_close"] = ref.reindex(starts + pd.Timedelta("15min")).values
    w["up"] = (w["ref_close"] >= w["ref_open"]).astype(int)
    w["ret"] = np.log(w["ref_close"] / w["ref_open"])
    # Path: close at each minute k=1..15 into the window (k=15 is the close).
    for k in range(1, 15):
        w[f"c{k}"] = m["close"].reindex(starts + pd.Timedelta(minutes=k) - pd.Timedelta("1min")).values
    vol = m["volume"].rolling(15).sum().reindex(starts + pd.Timedelta("15min") - pd.Timedelta("1min")).values
    w["vol_in_window"] = vol
    return w


if __name__ == "__main__":
    m = load_minutes(sys.argv[1])
    w = build_windows(m)
    print(m.index[0], m.index[-1], len(m), "minutes;", len(w), "windows")
    print("P(up) =", w["up"].mean().round(4), " zero-ret windows:", (w["ret"] == 0).mean().round(4))
    print("abs 15m move bp: median", (w["ret"].abs() * 1e4).median().round(2))
