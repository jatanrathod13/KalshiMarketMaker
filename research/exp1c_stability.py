"""Experiment 1c: stability of the beyond-mechanical edge by quarter, and which
features carry it."""
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

from exp1_ta_direction import _pipe, features, walk_forward
from windows import build_windows, load_minutes

m = load_minutes(sys.argv[1])
w = build_windows(m)
X = features(m, w).dropna()
y = w.loc[X.index, "up"]
spot = m["close"].reindex(X.index - pd.Timedelta("1min")).values
d = np.log(spot / w.loc[X.index, "ref_open"].values)
fv = pd.Series(norm.cdf(d / (np.maximum(X["rv60"].values, 1e-5) * np.sqrt(14 + 1 / 3))), index=X.index)
X2 = X.drop(columns=["r1"]).assign(fv_mech_probit=norm.ppf(fv.clip(1e-4, 1 - 1e-4)))
p = walk_forward(X2, y, _pipe)
ok = p.notna()
edge = (p - fv)[ok]
sel = edge.abs() >= 0.02
side_yes = edge[sel] > 0
price = pd.Series(np.where(side_yes, fv[ok][sel], 1 - fv[ok][sel]), index=edge[sel].index)
win = pd.Series(np.where(side_yes, y[ok][sel] == 1, y[ok][sel] == 0), index=edge[sel].index).astype(float)
gross = win - price
pnl = gross - 0.02 - 0.01  # 2c rounded 1-lot taker fee + 1c spread/slip
q = pnl.groupby(pnl.index.tz_localize(None).to_period("Q"))
print(pd.DataFrame({"n": q.size(), "gross_c": (gross.groupby(pnl.index.tz_localize(None).to_period("Q")).mean() * 100).round(2), "net_taker_c": (q.mean() * 100).round(2), "win": win.groupby(pnl.index.tz_localize(None).to_period("Q")).mean().round(4)}))
daily = pnl.groupby(pnl.index.date).sum()
print("days", len(daily), "pos-day share", round((daily > 0).mean(), 3), "daily mean c", round(daily.mean() * 100, 1),
      "daily sd c", round(daily.std() * 100, 1), "Sharpe/day", round(daily.mean() / daily.std(), 3))
# Block bootstrap (1-day blocks) of the mean per-trade P&L.
rng = np.random.default_rng(0)
days = np.array(list(daily.index))
g = {d_: pnl[pnl.index.date == d_].values for d_ in days}
boots = []
for _ in range(2000):
    pick = rng.choice(days, len(days))
    v = np.concatenate([g[d_] for d_ in pick])
    boots.append(v.mean())
print("per-trade P&L c: mean", round(pnl.mean() * 100, 2), " 95% block-bootstrap CI",
      np.round(np.percentile(boots, [2.5, 97.5]) * 100, 2))

# Coefficients of a fit on everything (standardized), to see what carries the edge.
mdl = _pipe().fit(X2, y)
coef = pd.Series(mdl.lr.coef_[0], index=X2.columns).sort_values()
print(coef.round(4).to_string())
