"""Pairs-trading analysis helpers.

Pure functions over plain Python lists. statsmodels is lazy-imported so
the rest of the codebase loads cleanly without it.

  * engle_granger(x, y) -> CointegrationFit
       Two-step: OLS y = α + β x; ADF on residuals.
  * ou_half_life(spread) -> float | None
       Fit AR(1) on Δspread vs spread_{t-1}; half-life = -ln(2)/β.
       Returns None if the series is not mean-reverting.
  * rolling_zscore(spread, window) -> list[float | None]
       z_t = (spread_t - mean_{t-W..t}) / std_{t-W..t}.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CointegrationFit:
    hedge_ratio: float
    intercept: float
    p_value: float
    residuals: list[float]
    cointegrated: bool


def engle_granger(
    x: list[float],
    y: list[float],
    *,
    p_threshold: float = 0.05,
) -> CointegrationFit:
    """Two-step Engle-Granger. Returns hedge ratio β so that y = α + β x + ε,
    and the ADF p-value of the residual series. `cointegrated` is True when
    p_value < p_threshold.
    """
    if len(x) != len(y):
        raise ValueError("x and y must be the same length")
    if len(x) < 30:
        raise ValueError("need at least 30 observations for cointegration")
    try:
        import numpy as np
        from statsmodels.tsa.stattools import adfuller
    except ImportError as exc:
        raise RuntimeError("statsmodels + numpy required for engle_granger") from exc

    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    # OLS via numpy: y = α + β x.
    a_matrix = np.column_stack([np.ones_like(x_arr), x_arr])
    coeffs, *_ = np.linalg.lstsq(a_matrix, y_arr, rcond=None)
    intercept, beta = float(coeffs[0]), float(coeffs[1])
    residuals = (y_arr - (intercept + beta * x_arr)).tolist()
    _adf_stat, p_value, *_ = adfuller(residuals, autolag="AIC")
    return CointegrationFit(
        hedge_ratio=beta,
        intercept=intercept,
        p_value=float(p_value),
        residuals=residuals,
        cointegrated=float(p_value) < p_threshold,
    )


def ou_half_life(spread: list[float]) -> float | None:
    """Half-life of mean reversion in bars.

    Fit Δs_t = α + β * s_{t-1} + ε. If β >= 0, no mean reversion -> None.
    Otherwise half_life = -ln(2) / ln(1 + β).
    """
    if len(spread) < 30:
        return None
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy required for ou_half_life") from exc

    s = np.asarray(spread, dtype=float)
    s_lag = s[:-1]
    ds = np.diff(s)
    a_matrix = np.column_stack([np.ones_like(s_lag), s_lag])
    coeffs, *_ = np.linalg.lstsq(a_matrix, ds, rcond=None)
    beta = float(coeffs[1])
    if beta >= 0:
        return None
    # 1 + β must be in (0, 1) for a proper half-life.
    decay = 1.0 + beta
    if decay <= 0 or decay >= 1:
        return None
    return -math.log(2.0) / math.log(decay)


def rolling_zscore(spread: list[float], window: int) -> list[float | None]:
    """Z-score of `spread` over a trailing `window`.

    Position i looks at spread[i-window+1 .. i] inclusive. Positions with
    less history than `window` return None.
    """
    if window <= 1:
        raise ValueError("window must be > 1")
    n = len(spread)
    out: list[float | None] = [None] * n
    for i in range(window - 1, n):
        seg = spread[i - window + 1 : i + 1]
        mean = sum(seg) / window
        var = sum((v - mean) ** 2 for v in seg) / (window - 1)
        std = math.sqrt(var)
        if std == 0:
            continue
        out[i] = (spread[i] - mean) / std
    return out


def correlation(x: list[float], y: list[float]) -> float:
    """Pearson correlation. Used as the cheap pre-screen before cointegration."""
    if len(x) != len(y):
        raise ValueError("x and y must be the same length")
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in x))
    dy = math.sqrt(sum((v - my) ** 2 for v in y))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)
