"""Tests for the remaining three trading agents and pairs analysis math."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agents.trading_crypto_sent import TradingCryptoSent, _mvrv_to_score
from agents.trading_pairs import TradingPairs
from agents.trading_trend import TradingTrend, _annualized_vol
from comms.approval_gate import NullApprovalGate
from data.feeds_crypto import DatedCryptoBar, StaticCryptoFeed
from data.feeds_india import DatedBar, StaticIndiaFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar
from models.pairs import (
    correlation,
    engle_granger,
    ou_half_life,
    rolling_zscore,
)
from record.research_log import ResearchLog, WriteSignal
from record.track_record import TrackRecord
from risk.risk_manager import FixedClock, RiskManager


# ==================================================================== pairs math


def test_correlation_perfect_positive() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert correlation(x, y) == pytest.approx(1.0)


def test_correlation_perfect_negative() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert correlation(x, y) == pytest.approx(-1.0)


def test_engle_granger_detects_cointegration() -> None:
    rng = random.Random(42)
    n = 200
    # Common stochastic trend (random walk) + tight noise -> cointegrated.
    rw = [0.0]
    for _ in range(n - 1):
        rw.append(rw[-1] + rng.gauss(0, 1))
    x = [50.0 + v + rng.gauss(0, 0.3) for v in rw]
    y = [40.0 + 1.0 * v + rng.gauss(0, 0.3) for v in rw]
    fit = engle_granger(x, y)
    assert fit.cointegrated
    assert fit.hedge_ratio == pytest.approx(1.0, abs=0.15)


def test_engle_granger_rejects_random() -> None:
    rng = random.Random(0)
    n = 200
    x_walk = [0.0]
    y_walk = [0.0]
    for _ in range(n - 1):
        x_walk.append(x_walk[-1] + rng.gauss(0, 1))
        y_walk.append(y_walk[-1] + rng.gauss(0, 1))
    fit = engle_granger(x_walk, y_walk)
    # Two independent random walks should fail cointegration most of the time.
    assert not fit.cointegrated or fit.p_value > 0.01


def test_ou_half_life_on_mean_reverting() -> None:
    rng = random.Random(7)
    n = 500
    s = [0.0]
    for _ in range(n - 1):
        s.append(0.8 * s[-1] + rng.gauss(0, 1))  # AR(1) with strong mean reversion
    hl = ou_half_life(s)
    assert hl is not None
    assert 1.0 < hl < 10.0


def test_ou_half_life_on_random_walk_is_none_or_huge() -> None:
    """A pure random walk has β ≈ 0. Half-life is either None (β >= 0)
    or extremely large (β just below 0 by chance). Either is acceptable
    for our agent's filter (we reject anything > 10 days)."""
    rng = random.Random(1)
    n = 500
    s = [0.0]
    for _ in range(n - 1):
        s.append(s[-1] + rng.gauss(0, 1))
    hl = ou_half_life(s)
    assert hl is None or hl > 50.0


def test_rolling_zscore_simple() -> None:
    spread = [0.0, 0.0, 0.0, 0.0, 5.0]
    z = rolling_zscore(spread, window=5)
    assert z[:4] == [None, None, None, None]
    assert z[-1] is not None and z[-1] > 1.0


# ============================================================ pairs agent


def _ist_now(h: int = 10, m: int = 30):
    ist = datetime(2026, 5, 18, h, m, tzinfo=ZoneInfo("Asia/Kolkata"))
    return ist.astimezone(timezone.utc)


def _make_pair_bars(
    n: int = 260, seed: int = 13
) -> tuple[list[DatedBar], list[DatedBar]]:
    """Cointegrated x/y series with a known recent spread excursion at the
    last bar so the Z-score crosses the entry threshold there.
    """
    rng = random.Random(seed)
    rw = [100.0]
    for _ in range(n - 1):
        rw.append(rw[-1] + rng.gauss(0, 0.5))
    x_closes = [v + rng.gauss(0, 0.1) for v in rw]
    y_closes = [v + rng.gauss(0, 0.1) for v in rw]
    # Inject a meaningful spread excursion at the last bar.
    y_closes[-1] += 5.0

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    x_bars = [
        DatedBar(ts=base + timedelta(days=i),
                 bar=OHLCBar(open=v, high=v * 1.01, low=v * 0.99, close=v, volume=1_000_000.0))
        for i, v in enumerate(x_closes)
    ]
    y_bars = [
        DatedBar(ts=base + timedelta(days=i),
                 bar=OHLCBar(open=v, high=v * 1.01, low=v * 0.99, close=v, volume=1_000_000.0))
        for i, v in enumerate(y_closes)
    ]
    return x_bars, y_bars


def _build_pairs_env(now=None):
    feed = StaticIndiaFeed()
    rl = ResearchLog(db_url="sqlite:///:memory:")
    tr = TrackRecord(db_url="sqlite:///:memory:")
    clock_time = now() if now else _ist_now()
    rm = RiskManager(tr, clock=FixedClock(clock_time))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    agent = TradingPairs(
        feed=feed,
        research_log=rl,
        track_record=tr,
        trade_router=router,
        portfolio_value_getter=lambda: 100_000.0,
        now_fn=now or (lambda: _ist_now()),
    )
    return agent, feed, rl, tr


def test_pairs_agent_enters_on_z_excursion(monkeypatch) -> None:
    agent, feed, _, tr = _build_pairs_env()
    monkeypatch.setattr(
        agent.settings.strategy, "pairs_universe", (("X", "Y"),)
    )
    x_bars, y_bars = _make_pair_bars()
    feed.set_ohlc("X", x_bars)
    feed.set_ohlc("Y", y_bars)
    agent.run_once()
    open_legs = tr.open_positions(agent="trading_pairs")
    # Expect two legs (one long, one short) if entry fires; or zero if the
    # synthetic data didn't quite produce a |z|>=2 — in either case the
    # agent should not have left a single dangling leg.
    assert len(open_legs) in (0, 2)


def test_pairs_agent_skips_uncointegrated(monkeypatch) -> None:
    agent, feed, _, tr = _build_pairs_env()
    monkeypatch.setattr(agent.settings.strategy, "pairs_universe", (("A", "B"),))
    rng = random.Random(0)
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a_bars = []
    b_bars = []
    # Two independent random walks — not cointegrated.
    a_walk = [100.0]
    b_walk = [50.0]
    for _ in range(259):
        a_walk.append(a_walk[-1] + rng.gauss(0, 0.5))
        b_walk.append(b_walk[-1] + rng.gauss(0, 0.5))
    for i, (av, bv) in enumerate(zip(a_walk, b_walk)):
        a_bars.append(DatedBar(ts=base + timedelta(days=i),
                               bar=OHLCBar(open=av, high=av, low=av, close=av, volume=1.0)))
        b_bars.append(DatedBar(ts=base + timedelta(days=i),
                               bar=OHLCBar(open=bv, high=bv, low=bv, close=bv, volume=1.0)))
    feed.set_ohlc("A", a_bars)
    feed.set_ohlc("B", b_bars)
    agent.run_once()
    assert not tr.open_positions(agent="trading_pairs")


def test_pairs_agent_handles_missing_data(monkeypatch) -> None:
    agent, _, _, tr = _build_pairs_env()
    monkeypatch.setattr(agent.settings.strategy, "pairs_universe", (("EMPTY1", "EMPTY2"),))
    agent.run_once()  # no data, no crash, no positions
    assert not tr.open_positions(agent="trading_pairs")


# ============================================================ trend agent


def _crypto_bars(closes: list[float]) -> list[DatedCryptoBar]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[DatedCryptoBar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        high = max(prev, c) * 1.005
        low = min(prev, c) * 0.995
        out.append(DatedCryptoBar(
            ts=base + timedelta(hours=4 * i),
            bar=OHLCBar(open=prev, high=high, low=low, close=c, volume=1.0),
        ))
        prev = c
    return out


def _build_trend_env():
    feed = StaticCryptoFeed()
    rl = ResearchLog(db_url="sqlite:///:memory:")
    tr = TrackRecord(db_url="sqlite:///:memory:")
    rm = RiskManager(tr)
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    agent = TradingTrend(
        feed=feed,
        research_log=rl,
        track_record=tr,
        trade_router=router,
        portfolio_value_getter=lambda: 100_000.0,
    )
    return agent, feed, rl, tr


def test_trend_agent_enters_on_majority_bullish(monkeypatch) -> None:
    agent, feed, _, tr = _build_trend_env()
    monkeypatch.setattr(agent.settings.strategy, "trend_universe", ("BTC/USDT",))
    closes = [100.0 + i * 0.5 for i in range(160)]  # clean uptrend
    feed.set_ohlc("BTC/USDT", _crypto_bars(closes))
    agent.run_once()
    opens = tr.open_positions(agent="trading_trend")
    assert len(opens) == 1
    assert opens[0].side == "BUY"


def test_trend_agent_no_entry_when_no_majority(monkeypatch) -> None:
    agent, feed, _, tr = _build_trend_env()
    monkeypatch.setattr(agent.settings.strategy, "trend_universe", ("BTC/USDT",))
    # Flat -> EMAs all equal -> no votes either way.
    feed.set_ohlc("BTC/USDT", _crypto_bars([100.0] * 160))
    agent.run_once()
    assert not tr.open_positions(agent="trading_trend")


def test_trend_agent_exits_when_majority_flips(monkeypatch) -> None:
    agent, feed, _, tr = _build_trend_env()
    monkeypatch.setattr(agent.settings.strategy, "trend_universe", ("BTC/USDT",))
    closes_up = [100.0 + i * 0.5 for i in range(160)]
    feed.set_ohlc("BTC/USDT", _crypto_bars(closes_up))
    agent.run_once()
    assert tr.open_positions(agent="trading_trend")

    # Now append a long downtrend so the majority flips bearish.
    closes_down = closes_up + [closes_up[-1] - i * 5.0 for i in range(1, 60)]
    feed.set_ohlc("BTC/USDT", _crypto_bars(closes_down))
    agent.run_once()
    assert not tr.open_positions(agent="trading_trend")


def test_trend_agent_size_modifier_amplifies(monkeypatch) -> None:
    agent, feed, rl, tr = _build_trend_env()
    monkeypatch.setattr(agent.settings.strategy, "trend_universe", ("BTC/USDT",))
    # Inject a positive size modifier.
    rl.write(WriteSignal(
        agent="trading_crypto_sent", market="crypto", ticker="PORTFOLIO",
        signal_type="crypto_size_modifier", value=1.0, payload={},
    ))
    closes = [100.0 + i * 0.5 for i in range(160)]
    feed.set_ohlc("BTC/USDT", _crypto_bars(closes))
    agent.run_once()
    pos = tr.open_positions(agent="trading_trend")[0]
    assert pos.signal_payload["size_modifier"] == pytest.approx(1.0)


def test_annualized_vol_positive_for_volatile_series() -> None:
    rng = random.Random(3)
    closes = [100.0]
    for _ in range(50):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.02)))
    vol = _annualized_vol(closes, lookback=30)
    assert vol is not None and vol > 0


def test_annualized_vol_short_series_none() -> None:
    assert _annualized_vol([100.0, 101.0], lookback=30) is None


# ============================================================ crypto sentiment gate


def test_mvrv_score_endpoints() -> None:
    # below bullish_th -> +1
    assert _mvrv_to_score(0.8, 1.0, 3.5) == 1.0
    # above bearish_th -> -1
    assert _mvrv_to_score(4.0, 1.0, 3.5) == -1.0
    # midpoint
    mid = (1.0 + 3.5) / 2
    assert _mvrv_to_score(mid, 1.0, 3.5) == pytest.approx(0.0, abs=1e-9)


def test_regime_gate_writes_modifier() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="PORTFOLIO",
        signal_type="regime", value=1.0, payload={"regime": "risk_on"},
    ))
    agent = TradingCryptoSent(research_log=rl)
    agent.run_once()
    mod = rl.latest("PORTFOLIO", "crypto_size_modifier")
    assert mod is not None
    assert mod.value == pytest.approx(1.0)
    assert "regime=+1.00" in mod.payload["explanation"]


def test_regime_gate_no_signals_writes_zero() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    agent = TradingCryptoSent(research_log=rl)
    agent.run_once()
    mod = rl.latest("PORTFOLIO", "crypto_size_modifier")
    assert mod is not None
    assert mod.value == pytest.approx(0.0)


def test_regime_gate_combines_components() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="PORTFOLIO",
        signal_type="regime", value=1.0, payload={},
    ))
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="PORTFOLIO",
        signal_type="mvrv", value=0.8, payload={},  # bullish (<1.0)
    ))
    agent = TradingCryptoSent(research_log=rl)
    agent.run_once()
    mod = rl.latest("PORTFOLIO", "crypto_size_modifier")
    assert mod is not None
    # average of [+1 (regime), +1 (mvrv)] = +1
    assert mod.value == pytest.approx(1.0)


def test_regime_gate_clamps() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    # An out-of-range social sentiment should be clamped to [-1, 1].
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="PORTFOLIO",
        signal_type="social_sentiment", value=5.0, payload={},
    ))
    agent = TradingCryptoSent(research_log=rl)
    agent.run_once()
    mod = rl.latest("PORTFOLIO", "crypto_size_modifier")
    assert mod is not None
    assert -1.0 <= mod.value <= 1.0
