"""End-to-end tests for the Indian momentum agent.

Uses real ResearchLog + TrackRecord + RiskManager + TradeRouter, a static
IndiaFeed for OHLC injection, and a fixed `now_fn` that puts us inside the
trading window. No mocks beyond the data sources.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agents.trading_momentum import TradingMomentum
from comms.approval_gate import NullApprovalGate
from data.feeds_india import DatedBar, StaticIndiaFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar
from record.research_log import ResearchLog, WriteSignal
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import FixedClock, RiskManager

# ----------------------------------------------------------- helpers


def _ist_clock_at(h: int, m: int):
    """Returns a callable that yields a UTC datetime corresponding to the
    given IST time on 2026-05-18 (a Monday — a trading day)."""
    ist = datetime(2026, 5, 18, h, m, tzinfo=ZoneInfo("Asia/Kolkata"))
    return lambda: ist.astimezone(timezone.utc)


def _bars_from_closes(closes: list[float], *, volume: float = 1_000_000.0) -> list[DatedBar]:
    """Synthesize daily OHLC bars from a close series. high/low band is tight
    so ATR stays predictable; volume defaults to a healthy constant.
    """
    out: list[DatedBar] = []
    base_day = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prev = closes[0]
    for i, c in enumerate(closes):
        high = max(prev, c) * 1.01
        low = min(prev, c) * 0.99
        out.append(DatedBar(
            ts=base_day + timedelta(days=i),
            bar=OHLCBar(open=prev, high=high, low=low, close=c, volume=volume),
        ))
        prev = c
    return out


def _build_env(*, now=None):
    feed = StaticIndiaFeed()
    rl = ResearchLog(db_url="sqlite:///:memory:")
    tr = TrackRecord(db_url="sqlite:///:memory:")
    rm = RiskManager(
        tr,
        clock=FixedClock(now() if now else datetime.now(timezone.utc)),
    )
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    agent = TradingMomentum(
        feed=feed,
        research_log=rl,
        track_record=tr,
        trade_router=router,
        portfolio_value_getter=lambda: 100_000.0,
        now_fn=now or (lambda: datetime.now(timezone.utc)),
    )
    return agent, feed, rl, tr


def _construct_closes_with_fresh_cross(
    *, period_fast: int = 8, period_slow: int = 32, period_trend: int = 200
) -> list[float]:
    """Build a close series where EMA(fast) crosses above EMA(slow) at the
    LAST bar. Provides at least `period_trend` bars and ends well above the
    200-EMA so the trend filter also passes.
    """
    from models.indicators import ewma as _ewma

    # Step 1: long uptrend to anchor the 200-EMA below current price.
    closes: list[float] = [50.0 + i * 0.5 for i in range(period_trend)]
    # Step 2: a brief dip so fast EMA falls below slow EMA.
    base = closes[-1]
    closes += [base - 2.0 - i * 1.0 for i in range(15)]
    # Step 3: sharp recovery; append bars until the cross lands on the last one.
    last = closes[-1]
    for _ in range(60):
        last += 3.0
        closes.append(last)
        f = _ewma(closes, period_fast)
        s = _ewma(closes, period_slow)
        if len(closes) < period_slow + 2 or f[-1] is None or s[-1] is None or f[-2] is None or s[-2] is None:
            continue
        if f[-2] <= s[-2] and f[-1] > s[-1]:
            return closes
    raise RuntimeError("could not construct a fresh cross series")


def _seed_uptrend(feed: StaticIndiaFeed, ticker: str, *, volume: float = 1_000_000.0) -> None:
    """Bullish-cross-at-last-bar series that also clears the ADX and trend filters."""
    closes = _construct_closes_with_fresh_cross()
    feed.set_ohlc(ticker, _bars_from_closes(closes, volume=volume))


def _seed_downtrend(feed: StaticIndiaFeed, ticker: str) -> None:
    closes = [200.0 - (i * 1.2) for i in range(80)]
    feed.set_ohlc(ticker, _bars_from_closes(closes))


def _seed_chop(feed: StaticIndiaFeed, ticker: str) -> None:
    closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(80)]
    feed.set_ohlc(ticker, _bars_from_closes(closes))


# ----------------------------------------------------------- session window


def test_runs_only_inside_trading_window() -> None:
    # 04:00 IST -> before market open.
    agent, feed, _, tr = _build_env(now=_ist_clock_at(4, 0))
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_skips_first_15_minutes() -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(9, 20))  # 9:15 + 5min
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


# ----------------------------------------------------------- entry filters


def test_enters_on_clean_uptrend(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    # Restrict universe so this one symbol drives the test.
    monkeypatch.setattr(
        agent.settings.strategy, "momentum_universe", ("HDFCBANK",), raising=True
    )
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    open_pos = tr.open_positions(agent="trading_momentum")
    assert len(open_pos) == 1
    pos = open_pos[0]
    assert pos.ticker == "HDFCBANK"
    payload = pos.signal_payload
    assert payload["strategy"] == "momentum_ewma_cross"
    assert payload["stop_price"] < pos.entry_price
    assert payload["target_price"] > pos.entry_price


def test_no_entry_when_no_cross(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    # Flat series — fast == slow always, no cross.
    feed.set_ohlc("HDFCBANK", _bars_from_closes([100.0] * 80))
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_no_entry_in_choppy_market(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_chop(feed, "HDFCBANK")
    agent.run_once()
    # Either no cross or ADX-below-threshold blocks it.
    assert not tr.open_positions(agent="trading_momentum")


def test_no_entry_when_volume_too_low(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_uptrend(feed, "HDFCBANK", volume=1_000_000.0)
    # Replace last bar with low-volume version.
    bars = feed._ohlc["HDFCBANK"]  # noqa: SLF001
    last = bars[-1]
    bars[-1] = DatedBar(
        ts=last.ts,
        bar=OHLCBar(
            open=last.bar.open, high=last.bar.high, low=last.bar.low,
            close=last.bar.close, volume=1.0,  # crashes below median
        ),
    )
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_no_entry_when_sentiment_negative(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_uptrend(feed, "HDFCBANK")
    # Negative sentiment in the research log.
    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=-0.4, payload={},
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_no_entry_when_insufficient_history(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    feed.set_ohlc("HDFCBANK", _bars_from_closes(list(range(1, 30))))
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_no_duplicate_entry(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    agent.run_once()
    assert len(tr.open_positions(agent="trading_momentum")) == 1


# ----------------------------------------------------------- exit logic


def test_stop_hit_closes_position(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    open_pos = tr.open_positions(agent="trading_momentum")[0]
    stop = open_pos.signal_payload["stop_price"]

    # Append a bar below the stop.
    bars = feed._ohlc["HDFCBANK"]  # noqa: SLF001
    last_ts = bars[-1].ts
    new_close = stop - 0.5
    bars.append(DatedBar(
        ts=last_ts + timedelta(days=1),
        bar=OHLCBar(open=new_close, high=new_close, low=new_close, close=new_close, volume=1_000_000.0),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_target_hit_closes_position(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    _seed_uptrend(feed, "HDFCBANK")
    agent.run_once()
    open_pos = tr.open_positions(agent="trading_momentum")[0]
    target = open_pos.signal_payload["target_price"]

    bars = feed._ohlc["HDFCBANK"]  # noqa: SLF001
    last_ts = bars[-1].ts
    new_close = target + 0.5
    bars.append(DatedBar(
        ts=last_ts + timedelta(days=1),
        bar=OHLCBar(open=new_close, high=new_close, low=new_close, close=new_close, volume=1_000_000.0),
    ))
    agent.run_once()
    closed = tr.closed_trades(agent="trading_momentum")
    assert closed and closed[0].pnl is not None and closed[0].pnl > 0


# ----------------------------------------------------------- robustness


def test_runs_with_no_data(monkeypatch) -> None:
    agent, _, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("HDFCBANK",))
    agent.run_once()
    assert not tr.open_positions(agent="trading_momentum")


def test_one_symbol_failure_does_not_block_others(monkeypatch) -> None:
    agent, feed, _, tr = _build_env(now=_ist_clock_at(10, 30))
    monkeypatch.setattr(agent.settings.strategy, "momentum_universe", ("BROKEN", "HDFCBANK"))
    _seed_uptrend(feed, "HDFCBANK")  # BROKEN has no data -> safely skipped
    agent.run_once()
    hdfc = [t for t in tr.open_positions(agent="trading_momentum") if t.ticker == "HDFCBANK"]
    assert len(hdfc) == 1
