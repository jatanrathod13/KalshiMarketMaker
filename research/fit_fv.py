"""Fit the calibrated in-window fair-value model and write its coefficients to
kalshi_market_maker/model/fv_coefficients.json."""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from exp2_inwindow_fv import design, naive_fv_table
from windows import build_windows, load_minutes



def fit_monotone(X, y):
    """Logistic fit with non-negative slopes: every feature rises with z, so the
    fitted probability is guaranteed to rise with spot."""
    def nll(theta):
        eta = theta[0] + X @ theta[1:]
        return np.mean(np.logaddexp(0, eta) - y * eta)

    def grad(theta):
        eta = theta[0] + X @ theta[1:]
        r = 1 / (1 + np.exp(-eta)) - y
        return np.concatenate([[r.mean()], X.T @ r / len(y)])

    bounds = [(None, None)] + [(0, None)] * X.shape[1]
    res = minimize(nll, np.r_[0.0, 1.0, 0.0, 0.0], jac=grad, bounds=bounds, method="L-BFGS-B")
    return float(res.x[0]), [float(c) for c in res.x[1:]]


if __name__ == "__main__":
    m = load_minutes(sys.argv[1])
    w = build_windows(m)
    df = naive_fv_table(m, w)
    out = {
        "description": "P(up) = logistic(b0 + b·[z, z^3/10, tanh z]) per whole minute elapsed; "
        "z = ln(spot/strike) / (sigma_1m * sqrt(14 - k + 1/3)); sigma_1m = EWMA(halflife 120 min) "
        "std of 1-minute log returns. Fit on Bitstamp BTC/USD 1-minute data.",
        "data_range": [str(w.index[0]), str(w.index[-1])],
        "ewma_halflife_minutes": 120,
        "by_minute": {},
    }
    for k in range(15):
        sub = df[df.k == k]
        b0, coef = fit_monotone(design(sub.z.values), sub.y.values)
        out["by_minute"][str(k)] = {"intercept": b0, "coef": coef, "n": int(len(sub))}
    path = Path(__file__).resolve().parents[1] / "kalshi_market_maker" / "model" / "fv_coefficients.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote", path)
