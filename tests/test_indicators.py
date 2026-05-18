"""Pure-function indicator tests with synthetic OHLC.

We use deterministic inputs (linear ramps, constant series, known-volatility
patterns) so the expected values are easy to reason about.
"""
from __future__ import annotations

import pytest

from models.indicators import OHLCBar, adx, atr, detect_cross, ewma, true_range


# ----------------------------------------------------------------- EWMA


def test_ewma_short_series_returns_none() -> None:
    assert ewma([1.0, 2.0, 3.0], period=5) == [None, None, None]


def test_ewma_seed_is_simple_average() -> None:
    out = ewma([1.0, 2.0, 3.0, 4.0, 5.0], period=5)
    assert out[:4] == [None, None, None, None]
    assert out[4] == pytest.approx(3.0)


def test_ewma_converges_on_constant_series() -> None:
    series = [10.0] * 30
    out = ewma(series, period=8)
    last = out[-1]
    assert last is not None
    assert last == pytest.approx(10.0, abs=1e-9)


def test_ewma_responds_to_uptrend() -> None:
    series = list(range(1, 31))  # 1..30
    out = ewma(series, period=8)
    last = out[-1]
    prev = out[-2]
    assert last is not None and prev is not None
    assert last > prev  # rising
    assert last < series[-1]  # but lags spot price (which is correct for EMA)


# ----------------------------------------------------------------- ATR


def _bar(open_: float, high: float, low: float, close: float, vol: float = 0.0) -> OHLCBar:
    return OHLCBar(open=open_, high=high, low=low, close=close, volume=vol)


def test_true_range_simple() -> None:
    prev_close = 100.0
    bar = _bar(102, 103, 99, 101)
    # range = 4, gap up = 3, gap down = 1 -> max 4
    assert true_range(prev_close, bar) == pytest.approx(4.0)


def test_atr_constant_volatility() -> None:
    # Each bar's TR is exactly 2. Wilder ATR(14) on a constant TR equals 2.
    bars = [_bar(100, 102, 100, 101)]
    for _ in range(20):
        bars.append(_bar(101, 103, 101, 102))  # high-low = 2, gaps small
    out = atr(bars, period=14)
    last = out[-1]
    assert last is not None
    assert last == pytest.approx(2.0, abs=0.2)


# ----------------------------------------------------------------- ADX


def test_adx_strong_uptrend_above_threshold() -> None:
    # Build a clean uptrend: each bar's high+low+close is higher than the prior.
    bars = []
    base = 100.0
    for i in range(60):
        h = base + 2
        l = base - 0.5
        c = base + 1.5
        bars.append(_bar(base, h, l, c))
        base += 1.0
    out = adx(bars, period=14)
    last = out[-1]
    assert last is not None
    assert last > 20.0


def test_adx_choppy_market_below_threshold() -> None:
    # Sawtooth: up-down-up-down. ADX should be low.
    bars = []
    base = 100.0
    for i in range(60):
        if i % 2 == 0:
            bars.append(_bar(base, base + 2, base - 0.5, base + 1.5))
            base += 0.05
        else:
            bars.append(_bar(base, base + 0.5, base - 2, base - 1.5))
            base -= 0.05
    out = adx(bars, period=14)
    last = out[-1]
    assert last is not None
    assert last < 30.0  # in chop, ADX hovers low


# ----------------------------------------------------------------- crossover


def test_detect_cross_bullish() -> None:
    fast = [None, 1.0, 2.0, 3.0, 5.0]
    slow = [None, 2.0, 2.5, 3.0, 4.0]
    # last pair: f=5 > s=4; prior: f=3 == s=3 (counts as <=) -> bullish
    assert detect_cross(fast, slow) == "bullish"


def test_detect_cross_bearish() -> None:
    # last pair must show fast crossing BELOW slow; prior must have fast >= slow.
    fast = [None, 5.0, 4.0, 3.5, 2.0]
    slow = [None, 4.0, 3.0, 3.0, 3.0]
    assert detect_cross(fast, slow) == "bearish"


def test_detect_cross_no_cross() -> None:
    fast = [1.0, 2.0, 3.0]
    slow = [5.0, 5.0, 5.0]
    assert detect_cross(fast, slow) is None


def test_detect_cross_insufficient_data() -> None:
    assert detect_cross([None], [None]) is None
    assert detect_cross([1.0], [2.0]) is None
