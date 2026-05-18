"""Backtest harness tests.

Synthetic OHLC + funding data so everything is deterministic. The key
invariant under test is no-future-leakage: at sim-time T, the historical
feeds must not return any bar with ts > T.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.research_crypto import ResearchCrypto
from agents.trading_funding import TradingFunding
from agents.trading_momentum import TradingMomentum
from backtest.clock import VirtualClock
from backtest.historical_feeds import HistoricalCryptoFeed, HistoricalIndiaFeed
from backtest.runner import BacktestRunner
from data.feeds_crypto import DatedCryptoBar, FundingPoint
from data.feeds_india import DatedBar
from models.indicators import OHLCBar
from record.research_log import ResearchLog
from record.track_record import TrackRecord

# ----------------------------------------------------------------- clock


def test_virtual_clock_set_and_advance() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    vc = VirtualClock(start)
    assert vc.now() == start
    vc.advance(timedelta(hours=2))
    assert vc.now() == start + timedelta(hours=2)
    vc.set(datetime(2026, 5, 1))                 # naive in -> aware out
    assert vc.now() == datetime(2026, 5, 1, tzinfo=timezone.utc)


# ----------------------------------------------------------------- feeds


def _crypto_bar(close: float, ts: datetime) -> DatedCryptoBar:
    return DatedCryptoBar(
        ts=ts,
        bar=OHLCBar(open=close, high=close * 1.005, low=close * 0.995, close=close, volume=1.0),
    )


def _india_bar(close: float, ts: datetime) -> DatedBar:
    return DatedBar(
        ts=ts,
        bar=OHLCBar(open=close, high=close * 1.01, low=close * 0.99, close=close, volume=1_000_000),
    )


def test_india_feed_does_not_reveal_future_bars() -> None:
    vc = VirtualClock(datetime(2026, 1, 5, tzinfo=timezone.utc))
    feed = HistoricalIndiaFeed(vc)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    feed.load_ohlc("HDFCBANK", [_india_bar(100 + i, base + timedelta(days=i)) for i in range(10)])
    visible = feed.fetch_ohlc("HDFCBANK", days=999)
    # clock is at day 5 -> bars 0..4 should be visible (5 total).
    assert len(visible) == 5
    assert [b.bar.close for b in visible] == [100, 101, 102, 103, 104]
    # advancing reveals one more bar.
    vc.advance(timedelta(days=1))
    assert len(feed.fetch_ohlc("HDFCBANK", days=999)) == 6


def test_india_feed_returns_empty_when_no_data() -> None:
    vc = VirtualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    feed = HistoricalIndiaFeed(vc)
    assert feed.fetch_ohlc("HDFCBANK") == []
    assert feed.fetch_latest_close("HDFCBANK") is None
    assert feed.fetch_news("HDFCBANK") == []


def test_crypto_feed_progressive_reveal() -> None:
    vc = VirtualClock(datetime(2026, 1, 1, 4, tzinfo=timezone.utc))
    feed = HistoricalCryptoFeed(vc)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    feed.load_ohlc("BTC/USDT", [_crypto_bar(60_000 + i * 100, base + timedelta(hours=i)) for i in range(10)])
    # clock at 04:00 -> 4 bars visible
    assert len(feed.fetch_ohlc("BTC/USDT")) == 5  # bars at 00,01,02,03,04
    vc.advance(timedelta(hours=2))
    assert len(feed.fetch_ohlc("BTC/USDT")) == 7


def test_crypto_funding_progressive_reveal() -> None:
    vc = VirtualClock(datetime(2026, 1, 1, 16, tzinfo=timezone.utc))
    feed = HistoricalCryptoFeed(vc)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [
        FundingPoint(symbol="BTC/USDT", rate=0.0001 + i * 1e-5,
                     funding_time=base + timedelta(hours=8 * i),
                     mark_price=60_000.0)
        for i in range(5)  # at 00, 08, 16, 24, 32 hours
    ]
    feed.load_funding_history("BTC/USDT", points)
    visible = feed.fetch_funding_history("BTC/USDT")
    # clock at 16:00 -> first three points visible (0, 8, 16).
    assert len(visible) == 3
    latest = feed.fetch_funding_rate("BTC/USDT")
    assert latest.funding_time == base + timedelta(hours=16)


def test_crypto_funding_raises_when_nothing_in_window() -> None:
    vc = VirtualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    feed = HistoricalCryptoFeed(vc)
    with pytest.raises(KeyError):
        feed.fetch_funding_rate("BTC/USDT")


# ----------------------------------------------------------------- runner


@pytest.fixture
def env(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'bt.db'}"
    tr = TrackRecord(db_url=db_url)
    rl = ResearchLog(db_url=db_url)
    vc = VirtualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    runner = BacktestRunner(clock=vc, track_record=tr, research_log=rl)
    return vc, tr, rl, runner


def test_runner_dispatches_agents_on_cadence(env, monkeypatch) -> None:
    vc, tr, rl, runner = env
    feed = HistoricalCryptoFeed(vc)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Funding rates well above the entry threshold so research_crypto has
    # something interesting to write but trading_funding still gates on
    # 3-stable-windows (it shouldn't enter without enough history).
    feed.load_funding_history("BTC/USDT", [
        FundingPoint(symbol="BTC/USDT", rate=0.0002,
                     funding_time=base + timedelta(hours=8 * i),
                     mark_price=60_000.0)
        for i in range(20)
    ])
    feed.set = None  # type: ignore[assignment]  # not needed for this test

    research = ResearchCrypto(feed=feed, research_log=rl)
    funding = TradingFunding(
        research_log=rl, track_record=tr, trade_router=runner.router,
        portfolio_value_getter=lambda: 100_000.0,
    )
    monkeypatch.setattr(funding.settings.strategy, "funding_universe", ("BTC/USDT",))

    result = runner.run(
        agents=[research, funding],
        start=base,
        end=base + timedelta(days=5),
        step=timedelta(hours=4),
    )
    assert result.agent_invocations["research_crypto"] > 0
    assert result.agent_invocations["trading_funding"] > 0
    # We expect at least one entry over 5 days of stable funding.
    closed_or_open = result.track_record.open_positions() + result.track_record.closed_trades()
    assert len(closed_or_open) >= 1, "trading_funding should have opened at least one carry"


def test_runner_handles_empty_universe(env) -> None:
    vc, _, _, runner = env
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = runner.run(
        agents=[],
        start=base,
        end=base + timedelta(days=2),
        step=timedelta(hours=1),
    )
    assert result.n_ticks > 0
    assert result.agent_invocations == {}


def test_runner_summary_shape(env) -> None:
    vc, _, _, runner = env
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = runner.run(
        agents=[],
        start=base,
        end=base + timedelta(days=1),
        step=timedelta(hours=1),
    )
    summary = result.summary()
    for key in ("start", "end", "step_seconds", "ticks", "agent_invocations",
                "trades_open", "trades_closed", "total_pnl"):
        assert key in summary


def test_runner_rejects_inverted_range(env) -> None:
    vc, _, _, runner = env
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        runner.run(agents=[], start=base, end=base - timedelta(hours=1))


# ----------------------------------------------------------------- no-future-leakage


def test_no_future_leakage_invariant(env) -> None:
    """Spy on every fetch_ohlc and confirm no returned bar has ts > clock.now()."""
    vc, tr, rl, runner = env
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    feed = HistoricalIndiaFeed(vc)
    feed.load_ohlc("HDFCBANK", [_india_bar(1500 + i, base + timedelta(days=i)) for i in range(60)])

    violations: list[tuple[datetime, datetime]] = []
    real_fetch = feed.fetch_ohlc

    def spied(ticker, *, days=60):
        bars = real_fetch(ticker, days=days)
        now = vc.now()
        for b in bars:
            ts = b.ts if b.ts.tzinfo is not None else b.ts.replace(tzinfo=timezone.utc)
            if ts > now:
                violations.append((now, ts))
        return bars

    feed.fetch_ohlc = spied  # type: ignore[method-assign]
    momentum = TradingMomentum(
        feed=feed, research_log=rl, track_record=tr,
        trade_router=runner.router,
        portfolio_value_getter=lambda: 100_000.0,
    )
    runner.run(
        agents=[momentum],
        start=base + timedelta(days=30),
        end=base + timedelta(days=55),
        step=timedelta(hours=6),
    )
    assert violations == [], f"future-leakage detected at {violations[:3]}"
