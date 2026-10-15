"""Experiment 1b: how much of the open-time edge is the *mechanical* strike gap
(spot at open vs the 60 s average that becomes the strike), which any competent
Kalshi market maker prices in, and how much is extra (TA / mean reversion)?"""
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.metrics import log_loss

from exp1_ta_direction import _pipe, features, report, walk_forward
from windows import build_windows, load_minutes

m = load_minutes(sys.argv[1])
w = build_windows(m)
X = features(m, w).dropna()
y = w.loc[X.index, "up"]

# Mechanical fair value: d = ln(spot_T / strike); remaining move to the final
# 60 s average has variance ≈ σ²·(14 + 1/3) minutes for a random walk.
spot = m["close"].reindex(X.index - pd.Timedelta("1min")).values
strike = w.loc[X.index, "ref_open"].values
d = np.log(spot / strike)
sigma_min = np.maximum(X["rv60"].values, 1e-5)
fv_mech = pd.Series(norm.cdf(d / (sigma_min * np.sqrt(14 + 1 / 3))), index=X.index)
report("mechanical FV only (no fitting)", fv_mech, y)
base_ll = log_loss(y, fv_mech.clip(1e-6, 1 - 1e-6))

X2 = X.copy()
X2["fv_mech_probit"] = norm.ppf(fv_mech.clip(1e-4, 1 - 1e-4))
ta_cols = [c for c in X.columns if c not in ("r1",)]
full = walk_forward(X2[ta_cols + ["fv_mech_probit"]], y, _pipe)
report("logistic: mechanical FV + TA", full, y)

ok = full.notna()
print("\nlogloss on same OOS rows: mech", round(log_loss(y[ok], fv_mech[ok].clip(1e-6, 1 - 1e-6)), 5),
      " mech+TA", round(log_loss(y[ok], full[ok]), 5))

# Edge vs a market that prices the mechanical FV exactly: buy the side the model
# favours whenever model and mech FV disagree by >= k, paying the mech price.
for k in (0.0, 0.02, 0.04, 0.06):
    edge = full[ok] - fv_mech[ok]
    sel = edge.abs() >= k
    side_yes = edge[sel] > 0
    price = np.where(side_yes, fv_mech[ok][sel], 1 - fv_mech[ok][sel])
    win = np.where(side_yes, y[ok][sel] == 1, y[ok][sel] == 0)
    fee = np.ceil(0.07 * price * (1 - price) * 100) / 100  # 1-contract taker fee, rounded up
    print(f"edge>={k:.2f}: n={sel.sum():6d} win={win.mean():.4f} avg price={price.mean():.3f} "
          f"gross={100 * (win - price).mean():+.2f}c  after 1-lot taker fee={100 * (win - price - fee).mean():+.2f}c "
          f"after fee+1c slip={100 * (win - price - fee - 0.01).mean():+.2f}c")
