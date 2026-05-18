"""Pure-functional technical indicators.

No pandas / numpy dependency — operate on plain lists. Easy to test with
synthetic data and easy to keep deterministic across CPU/Python versions.

Conventions:
  * `period` is the lookback in bars.
  * EMA seeds with the simple average of the first `period` values, then
    rolls forward with the standard alpha = 2/(period+1) recurrence.
  * ATR uses Wilder smoothing (alpha = 1/period), same as J.W. Wilder's
    original paper — most trading platforms use this.
  * ADX uses Wilder smoothing for +DI / -DI / DX.

All functions return a list aligned to the input — the leading positions
that don't have enough history are filled with `None`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OHLCBar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# --------------------------------------------------------------------- EWMA


def ewma(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average.

    First `period - 1` outputs are None. Position `period - 1` is the
    simple average of the first `period` values (the seed). After that,
    EMA_t = alpha * x_t + (1 - alpha) * EMA_{t-1}.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


# --------------------------------------------------------------------- ATR


def true_range(prev_close: float, bar: OHLCBar) -> float:
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: list[OHLCBar], period: int = 14) -> list[float | None]:
    """Wilder ATR. First `period` positions are None."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs = [0.0]  # first bar has no prev_close; we'll skip index 0 anyway
    for i in range(1, n):
        trs.append(true_range(bars[i - 1].close, bars[i]))
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# --------------------------------------------------------------------- ADX


def adx(bars: list[OHLCBar], period: int = 14) -> list[float | None]:
    """Wilder ADX. Returns ADX value per bar; first 2*period positions None."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < 2 * period + 1:
        return out

    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    tr_list: list[float] = [0.0]
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr_list.append(true_range(bars[i - 1].close, bars[i]))

    # Wilder smoothing: initial sum of first `period`, then prev - prev/period + new.
    smoothed_plus = sum(plus_dm[1 : period + 1])
    smoothed_minus = sum(minus_dm[1 : period + 1])
    smoothed_tr = sum(tr_list[1 : period + 1])

    dx_series: list[float | None] = [None] * n
    for i in range(period, n):
        if i > period:
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[i]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i]
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        if smoothed_tr == 0:
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denom = plus_di + minus_di
        if denom == 0:
            dx_series[i] = 0.0
        else:
            dx_series[i] = 100.0 * abs(plus_di - minus_di) / denom

    # ADX = Wilder-smoothed DX, starting at index 2*period.
    first_adx_at = 2 * period
    dx_window: list[float] = [
        v for v in (dx_series[i] for i in range(period, first_adx_at + 1)) if v is not None
    ]
    if len(dx_window) < period + 1:
        return out
    adx_seed = sum(dx_window[:period]) / period
    out[first_adx_at] = adx_seed
    prev = adx_seed
    for i in range(first_adx_at + 1, n):
        dx_i = dx_series[i]
        if dx_i is None:
            continue
        prev = (prev * (period - 1) + dx_i) / period
        out[i] = prev
    return out


# -------------------------------------------------------------- crossover helper


def detect_cross(fast: list[float | None], slow: list[float | None]) -> str | None:
    """Look at the last two non-None aligned pairs.

    Returns "bullish" if fast crossed above slow, "bearish" if fast crossed
    below, or None if no crossover at the most recent bar.
    """
    pairs: list[tuple[float, float]] = [
        (f, s) for f, s in zip(fast, slow) if f is not None and s is not None
    ]
    if len(pairs) < 2:
        return None
    f_prev, s_prev = pairs[-2]
    f_now, s_now = pairs[-1]
    if f_prev <= s_prev and f_now > s_now:
        return "bullish"
    if f_prev >= s_prev and f_now < s_now:
        return "bearish"
    return None
