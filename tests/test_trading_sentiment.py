"""End-to-end tests for the sentiment-trading agent.

Real ResearchLog + TrackRecord + RiskManager + TradeRouter (in-memory SQLite),
StaticIndiaFeed for OHLC + price injection, NullApprovalGate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agents.trading_sentiment import TradingSentiment, _decay_weighted
from comms.approval_gate import NullApprovalGate
from data.feeds_india import DatedBar, StaticIndiaFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar
from record.research_log import ResearchLog, SignalRecord, WriteSignal
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import FixedClock, RiskManager

# ----------------------------------------------------------- helpers

IST_OPEN = datetime(2026, 5, 18, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))


def _now_fn(at: datetime = IST_OPEN):
    utc = at.astimezone(timezone.utc)
    return lambda: utc


def _bars(closes: list[float], *, volume: float = 1_000_000.0) -> list[DatedBar]:
    out: list[DatedBar] = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prev = closes[0]
    for i, c in enumerate(closes):
        high = max(prev, c) * 1.01
        low = min(prev, c) * 0.99
        out.append(DatedBar(
            ts=base + timedelta(days=i),
            bar=OHLCBar(open=prev, high=high, low=low, close=c, volume=volume),
        ))
        prev = c
    return out


def _build_env(*, now=None):
    feed = StaticIndiaFeed()
    rl = ResearchLog(db_url="sqlite:///:memory:")
    tr = TrackRecord(db_url="sqlite:///:memory:")
    rm = RiskManager(tr, clock=FixedClock(now() if now else datetime.now(timezone.utc)))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    agent = TradingSentiment(
        feed=feed,
        research_log=rl,
        track_record=tr,
        trade_router=router,
        portfolio_value_getter=lambda: 100_000.0,
        now_fn=now or (lambda: datetime.now(timezone.utc)),
    )
    return agent, feed, rl, tr


def _seed_steady_prices(feed: StaticIndiaFeed, ticker: str, *, price: float = 1500.0) -> None:
    # Just enough bars for ATR + buffer. Gentle drift so ATR > 0.
    closes = [price + (i % 5 - 2) * 0.3 for i in range(70)]
    feed.set_ohlc(ticker, _bars(closes))


def _seed_sentiment(
    rl: ResearchLog,
    ticker: str,
    values: list[float],
    *,
    headline_count: int = 4,
    base_ts: datetime | None = None,
) -> None:
    """Write sentiment_score records (chronological — first is oldest)."""
    base = base_ts or datetime.now(timezone.utc) - timedelta(minutes=15 * len(values))
    for i, v in enumerate(values):
        ts = base + timedelta(minutes=15 * i)
        rl.write(WriteSignal(
            agent="research_india",
            market="india",
            ticker=ticker,
            signal_type="sentiment_score",
            value=v,
            payload={"headline_count": headline_count},
            ts=ts,
        ))


# ----------------------------------------------------------- entry tests


def test_enters_on_sustained_high_sentiment(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    opens = [t for t in tr.open_positions(agent="trading_sentiment") if t.ticker == "HDFCBANK"]
    assert len(opens) == 1
    pay = opens[0].signal_payload
    assert pay["strategy"] == "sentiment_breakout"
    assert pay["stop_price"] < opens[0].entry_price
    assert pay["target_price"] > opens[0].entry_price


def test_does_not_enter_on_single_high_print(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.3, 0.4, 0.85])  # only the latest crosses
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_does_not_enter_when_too_few_headlines(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    # 3 strong prints but only 1 headline each — fails Rule 5.
    _seed_sentiment(rl, "HDFCBANK", [0.80, 0.82, 0.85], headline_count=1)
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_does_not_enter_when_decay_weighted_below_threshold(monkeypatch) -> None:
    """Latest record is hot, but older records are neutral — decay-weighted
    average drags below threshold and the entry is blocked. Catches stale
    signals where a single recent positive headline rides on a neutral past.
    """
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    monkeypatch.setattr(agent.settings.strategy, "sentiment_decay_halflife_hours", 6.0)
    _seed_steady_prices(feed, "HDFCBANK")
    # Order: writes oldest first. Newest record is hot (0.80) but older
    # records are neutral (0.10). Decay weights the latest most heavily,
    # but with halflife=6h and our 15-min spacing the older two still
    # contribute enough to pull the weighted avg below 0.72.
    base_old = IST_OPEN.astimezone(timezone.utc) - timedelta(hours=12)
    _seed_sentiment(rl, "HDFCBANK", [0.10, 0.10, 0.80], base_ts=base_old)
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_does_not_enter_when_volume_low(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    # Crash latest volume.
    bars = feed._ohlc["HDFCBANK"]  # noqa: SLF001
    last = bars[-1]
    bars[-1] = DatedBar(
        ts=last.ts,
        bar=OHLCBar(
            open=last.bar.open, high=last.bar.high, low=last.bar.low,
            close=last.bar.close, volume=1.0,
        ),
    )
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_no_duplicate_entry(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    agent.run_once()
    assert len(tr.open_positions(agent="trading_sentiment")) == 1


# ----------------------------------------------------------- exit tests


def test_panic_exit_on_negative_news(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    assert tr.open_positions(agent="trading_sentiment")

    # Major negative headline arrives.
    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=-0.5, payload={"headline_count": 5},
        ts=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_fade_exit_when_sentiment_drops(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()

    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=0.30, payload={"headline_count": 4},
        ts=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_max_holding_exit(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    assert tr.open_positions(agent="trading_sentiment")

    # Jump the clock forward past the max-holding window.
    future = IST_OPEN.astimezone(timezone.utc) + timedelta(days=6)
    agent._now = lambda: future  # noqa: SLF001

    # Sentiment is still hot, so only the max-hold rule should fire.
    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=0.80, payload={"headline_count": 5},
        ts=future - timedelta(minutes=1),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_stop_hit_closes(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    pos = tr.open_positions(agent="trading_sentiment")[0]
    stop = pos.signal_payload["stop_price"]

    bars = feed._ohlc["HDFCBANK"]  # noqa: SLF001
    last_ts = bars[-1].ts
    new_close = stop - 0.5
    bars.append(DatedBar(
        ts=last_ts + timedelta(days=1),
        bar=OHLCBar(open=new_close, high=new_close, low=new_close, close=new_close, volume=1_000_000.0),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_cooldown_blocks_immediate_reentry(monkeypatch) -> None:
    agent, feed, rl, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    pos = tr.open_positions(agent="trading_sentiment")[0]
    tr.close_trade(CloseTradeRequest(trade_id=pos.id, exit_price=pos.entry_price))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


# ----------------------------------------------------------- decay math


def test_decay_weighted_pure_function() -> None:
    now = datetime.now(timezone.utc)

    class _Rec:
        def __init__(self, value: float, age_h: float) -> None:
            self.value = value
            self.ts = now - timedelta(hours=age_h)
            self.payload = {}

    # halflife 1h, values [1, 1, 1] at ages 0/1/2 -> weights 1, 0.5, 0.25
    out = _decay_weighted([_Rec(1.0, 0), _Rec(1.0, 1), _Rec(1.0, 2)], 1.0, now)
    assert out == pytest.approx(1.0)

    # Recent positive, old strongly negative — recent should dominate.
    out = _decay_weighted([_Rec(0.9, 0), _Rec(-1.0, 24)], 1.0, now)
    assert out > 0.5


def test_decay_weighted_empty() -> None:
    assert _decay_weighted([], 1.0, datetime.now(timezone.utc)) == 0.0


# ----------------------------------------------------------- robustness


def test_outside_trading_window_no_action(monkeypatch) -> None:
    early = datetime(2026, 5, 18, 4, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    agent, feed, rl, tr = _build_env(now=_now_fn(early))
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    _seed_steady_prices(feed, "HDFCBANK")
    _seed_sentiment(rl, "HDFCBANK", [0.75, 0.78, 0.80])
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")


def test_no_sentiment_history_safe(monkeypatch) -> None:
    agent, _, _, tr = _build_env(now=_now_fn())
    monkeypatch.setattr(agent.settings.strategy, "sentiment_universe", ("HDFCBANK",))
    agent.run_once()
    assert not tr.open_positions(agent="trading_sentiment")
