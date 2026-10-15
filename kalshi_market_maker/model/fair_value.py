"""Calibrated fair value for 15-minute crypto up/down markets (KXBTC15M style).

The contract resolves YES when the 60 s average of the index before close is at
least the 60 s average before open (the strike). A plain random-walk digital
model is miscalibrated by up to ~12c late in the window on historical BTC data,
so this model applies a per-minute monotone calibration fitted by
``research/fit_fv.py``. Out of sample (fit on 2025, tested on 2026) the
calibrated model's worst probability bucket is off by ~1-3.5c for minutes 0-12.
"""

import json
import math
from pathlib import Path
from typing import Dict, Optional

_COEFFICIENTS_PATH = Path(__file__).with_name("fv_coefficients.json")


class EwmaVol:
    """EWMA standard deviation of 1-minute log returns (feed it minute closes)."""

    def __init__(self, halflife_minutes: float = 120.0, floor: float = 1e-5):
        self.alpha = 1 - 0.5 ** (1 / halflife_minutes)
        self.floor = floor
        self.variance: Optional[float] = None
        self.last_close: Optional[float] = None

    def update(self, minute_close: float) -> None:
        if self.last_close is not None and minute_close > 0 and self.last_close > 0:
            r = math.log(minute_close / self.last_close)
            if self.variance is None:
                self.variance = r * r
            else:
                self.variance = (1 - self.alpha) * self.variance + self.alpha * r * r
        self.last_close = minute_close

    @property
    def sigma(self) -> Optional[float]:
        if self.variance is None:
            return None
        return max(math.sqrt(self.variance), self.floor)


def _design(z: float):
    zc = max(-8.0, min(8.0, z))
    return (zc, zc ** 3 / 10, math.tanh(zc))


class FairValueModel:
    def __init__(self, coefficients: Optional[Dict] = None):
        if coefficients is None:
            coefficients = json.loads(_COEFFICIENTS_PATH.read_text())
        self.by_minute = {int(k): v for k, v in coefficients["by_minute"].items()}
        self.max_minute = max(self.by_minute)

    @staticmethod
    def z_score(spot: float, strike: float, sigma_1m: float, minutes_elapsed: float) -> float:
        remaining = max(14.0 - minutes_elapsed, 0.0) + 1 / 3
        return math.log(spot / strike) / (sigma_1m * math.sqrt(remaining))

    def _prob_at_minute(self, k: int, z: float) -> float:
        params = self.by_minute[k]
        eta = params["intercept"] + sum(c * x for c, x in zip(params["coef"], _design(z)))
        return 1 / (1 + math.exp(-eta))

    def p_yes(self, spot: float, strike: float, sigma_1m: float, minutes_elapsed: float) -> float:
        """Probability the window settles YES (up), for 0 <= minutes_elapsed <= 14."""
        if not 0 <= minutes_elapsed <= self.max_minute:
            raise ValueError(f"minutes_elapsed must be within [0, {self.max_minute}], got {minutes_elapsed}")
        z = self.z_score(spot, strike, sigma_1m, minutes_elapsed)
        lo = int(math.floor(minutes_elapsed))
        hi = min(lo + 1, self.max_minute)
        frac = minutes_elapsed - lo
        return (1 - frac) * self._prob_at_minute(lo, z) + frac * self._prob_at_minute(hi, z)
