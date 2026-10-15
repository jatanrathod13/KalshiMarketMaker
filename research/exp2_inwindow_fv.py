"""Experiment 2: in-window fair value. At minute k of a window, is the
random-walk digital model (with the 60 s settlement average) calibrated? Then
fit a calibrated version on 2025 and test it on 2026."""
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from windows import build_windows, load_minutes


def naive_fv_table(m: pd.DataFrame, w: pd.DataFrame) -> pd.DataFrame:
    r1 = np.log(m["close"]).diff()
    # σ per minute, EWMA with a 2 h half-life, known at window open.
    sig = np.maximum(r1.ewm(halflife=120).std().reindex(w.index - pd.Timedelta("1min")).values, 1e-5)
    rows = []
    for k in range(0, 15):
        spot = m["close"].reindex(w.index + pd.Timedelta(minutes=k) - pd.Timedelta("1min")).values
        d = np.log(spot / w["ref_open"].values)
        remaining = (14 - k) + 1 / 3
        z = d / (sig * np.sqrt(remaining))
        rows.append(pd.DataFrame({"t0": w.index, "k": k, "z": z, "p": norm.cdf(z), "y": w["up"].values}))
    return pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan).dropna()


def design(z: np.ndarray) -> np.ndarray:
    zc = np.clip(z, -8, 8)
    return np.column_stack([zc, zc ** 3 / 10, np.tanh(zc)])


if __name__ == "__main__":
    m = load_minutes(sys.argv[1])
    w = build_windows(m)
    df = naive_fv_table(m, w)
    bins = [0, .03, .1, .2, .3, .4, .5, .6, .7, .8, .9, .97, 1]
    df["bin"] = pd.cut(df["p"], bins)
    tab = df.groupby(["k", "bin"], observed=True).agg(n=("y", "size"), model=("p", "mean"), actual=("y", "mean"))
    tab["gap_c"] = ((tab["actual"] - tab["model"]) * 100).round(2)
    for k in (0, 3, 6, 9, 12, 13, 14):
        print(f"\n--- minute {k} ({15 - k} min left): naive model vs actual ---")
        print(tab.loc[k][["n", "model", "actual", "gap_c"]].round(3).to_string())

    from fit_fv import fit_monotone

    split = pd.Timestamp("2026-01-01", tz="UTC")
    print("\nCalibrated FV (fit < 2026, test ≥ 2026): logloss naive → calibrated, and worst bin gap after")
    for k in range(0, 15):
        sub = df[df.k == k]
        tr, te = sub[sub.t0 < split], sub[sub.t0 >= split]
        b0, coef = fit_monotone(design(tr.z.values), tr.y.values)
        p_new = 1 / (1 + np.exp(-(b0 + design(te.z.values) @ np.array(coef))))
        ll0 = log_loss(te.y, te.p.clip(1e-6, 1 - 1e-6))
        ll1 = log_loss(te.y, p_new)
        b = pd.cut(p_new, bins)
        gaps = pd.DataFrame({"p": p_new, "y": te.y.values, "b": b}).groupby("b", observed=True).agg(
            n=("y", "size"), p=("p", "mean"), y=("y", "mean"))
        gaps = gaps[gaps.n >= 200]
        worst = ((gaps.y - gaps.p).abs().max() * 100)
        print(f"k={k:2d}: {ll0:.4f} → {ll1:.4f}   worst-bin |gap| after = {worst:.2f}c")
