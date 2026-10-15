"""Experiment 1: do technical indicators known at window open predict the
15-minute up/down outcome? Walk-forward, monthly re-fit, out-of-sample only."""
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from sklearn.preprocessing import StandardScaler

from windows import build_windows, load_minutes


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(m: pd.DataFrame, w: pd.DataFrame) -> pd.DataFrame:
    # Everything is indexed by candle open time; a value at T-1m is known at T.
    c = m["close"]
    lc = np.log(c)
    f = pd.DataFrame(index=m.index)
    for n in (1, 5, 15, 60, 240, 1440):
        f[f"r{n}"] = lc - lc.shift(n)
    r1 = lc.diff()
    f["rv60"] = r1.rolling(60).std()
    f["rv_ratio"] = r1.rolling(15).std() / r1.rolling(240).std()
    f["rsi1m"] = rsi(c, 14)
    c15 = c.resample("15min", label="right", closed="right").last()  # placeholder alignment below
    v = m["volume"]
    f["vol_z"] = (v.rolling(60).sum() - v.rolling(1440).sum() / 24) / (v.rolling(1440).sum() / 24 + 1e-9)
    f["dist_hi24"] = lc - np.log(m["high"].rolling(1440).max())
    f["dist_lo24"] = lc - np.log(m["low"].rolling(1440).min())
    f["vwap_dev"] = lc - np.log((m["typ"] * v).rolling(240).sum() / (v.rolling(240).sum() + 1e-9))
    known = f.shift(0).reindex(w.index - pd.Timedelta("1min"))
    known.index = w.index
    # 15-minute bar indicators on completed windows only.
    wc = w["ref_close"].shift(1)  # previous window's settlement value, known at open
    known["prev_ret"] = w["ret"].shift(1)
    known["prev2_ret"] = w["ret"].shift(2)
    known["rsi15"] = rsi(wc, 14)
    e9, e21 = wc.ewm(span=9).mean(), wc.ewm(span=21).mean()
    known["ema_9_21"] = np.log(e9 / e21)
    mu, sd = wc.rolling(20).mean(), wc.rolling(20).std()
    known["boll_z"] = (wc - mu) / sd
    known["streak"] = (
        np.sign(w["ret"].shift(1)).groupby((np.sign(w["ret"].shift(1)) != np.sign(w["ret"].shift(2))).cumsum()).cumcount() + 1
    ) * np.sign(w["ret"].shift(1))
    h = w.index.hour + w.index.minute / 60
    known["hour_sin"], known["hour_cos"] = np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24)
    known["dow"] = w.index.dayofweek
    return known.replace([np.inf, -np.inf], np.nan)


def walk_forward(X: pd.DataFrame, y: pd.Series, make_model, min_train_months=6):
    months = X.index.tz_localize(None).to_period("M")
    uniq = months.unique()
    preds = pd.Series(np.nan, index=X.index)
    for i in range(min_train_months, len(uniq)):
        tr = months < uniq[i]
        te = months == uniq[i]
        model = make_model()
        model.fit(X[tr], y[tr])
        preds[te] = model.predict_proba(X[te])[:, 1]
    return preds


def report(name, p, y, entry_costs=(0.0, 0.01, 0.0275)):
    ok = p.notna()
    p, y = p[ok], y[ok]
    ll = log_loss(y, p.clip(1e-6, 1 - 1e-6))
    base = log_loss(y, np.full(len(y), y.mean()))
    acc = ((p > 0.5) == (y == 1)).mean()
    print(f"\n[{name}] n={len(y)}  acc={acc:.4f}  logloss={ll:.5f} vs base {base:.5f}  (Δ={base - ll:+.5f})")
    for thr in (0.50, 0.52, 0.55, 0.58):
        sel = (p - 0.5).abs() >= thr - 0.5
        if sel.sum() < 50:
            continue
        hit = ((p[sel] > 0.5) == (y[sel] == 1))
        bt = binomtest(int(hit.sum()), int(sel.sum()), 0.5, alternative="greater")
        pnl = {c: (hit.astype(float) - (0.5 + c)).mean() * 100 for c in entry_costs}
        pnl_s = "  ".join(f"cost {c * 100:.2f}c→{v:+.2f}c/ct" for c, v in pnl.items())
        print(f"  |p-.5|>={thr - 0.5:.2f}: trades={sel.sum():6d} hit={hit.mean():.4f} p={bt.pvalue:.3g}  {pnl_s}")


class _Pipe:
    def __init__(self):
        self.sc, self.lr = StandardScaler(), LogisticRegression(C=0.05, max_iter=2000)

    def fit(self, X, y):
        self.lr.fit(self.sc.fit_transform(X), y)
        return self

    def predict_proba(self, X):
        return self.lr.predict_proba(self.sc.transform(X))


def _pipe():
    return _Pipe()


if __name__ == "__main__":
    m = load_minutes(sys.argv[1])
    w = build_windows(m)
    X = features(m, w).dropna()
    y = w.loc[X.index, "up"]
    print("windows with features:", len(X), X.index[0], "→", X.index[-1])

    # Single-signal sanity checks (no fitting): sign rules.
    for col in ("prev_ret", "r5", "r15", "r60", "ema_9_21", "boll_z", "rsi15", "rsi1m", "vwap_dev"):
        s = X[col] - (50 if col.startswith("rsi") else 0)
        for direction, label in ((1, "follow"), (-1, "fade")):
            pred = (np.sign(s) * direction > 0)
            hit = (pred.astype(int) == y).mean()
            print(f"rule {label:6s} {col:9s}: hit={hit:.4f}")
    corr = X.corrwith(w.loc[X.index, "ret"], method="spearman").sort_values()
    print("\nSpearman corr with next-window return:\n", corr.round(4).to_string())

    logit = walk_forward(X, y, lambda: _pipe())
    report("logistic", logit, y)
    gbm = walk_forward(
        X, y,
        lambda: lgb.LGBMClassifier(n_estimators=200, learning_rate=0.02, num_leaves=15, min_child_samples=400,
                                   subsample=0.7, subsample_freq=1, colsample_bytree=0.7, verbose=-1),
    )
    report("lightgbm", gbm, y)
