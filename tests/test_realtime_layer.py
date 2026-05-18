"""Tests for the real-time layer: SignalBus, CandleBuilder, NewsPoller."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.news_poller import NewsPoller
from data.feeds_india import NewsItem, StaticIndiaFeed
from infra.signal_bus import InMemoryBus
from models.candle_builder import CandleBuilder
from models.finbert_scorer import NullScorer, SentimentScore
from record.research_log import ResearchLog


# ====================================================================== SignalBus


def test_inmemory_bus_delivers_to_subscriber() -> None:
    bus = InMemoryBus()
    try:
        received: list[tuple[str, dict]] = []
        bus.subscribe("price.BTC", lambda ch, p: received.append((ch, p)))
        bus.publish("price.BTC", {"close": 60_000})
        bus.drain()
        assert received == [("price.BTC", {"close": 60_000})]
    finally:
        bus.shutdown()


def test_inmemory_bus_fanout() -> None:
    bus = InMemoryBus()
    try:
        counts = {"a": 0, "b": 0}
        bus.subscribe("news.alert", lambda _c, _p: counts.__setitem__("a", counts["a"] + 1))
        bus.subscribe("news.alert", lambda _c, _p: counts.__setitem__("b", counts["b"] + 1))
        for _ in range(3):
            bus.publish("news.alert", {})
        bus.drain()
        assert counts == {"a": 3, "b": 3}
    finally:
        bus.shutdown()


def test_inmemory_bus_isolates_subscriber_errors() -> None:
    bus = InMemoryBus()
    try:
        good: list[str] = []

        def _bad(_c, _p):
            raise RuntimeError("bad subscriber")

        bus.subscribe("ch", _bad)
        bus.subscribe("ch", lambda _c, _p: good.append("ok"))
        bus.publish("ch", "msg")
        bus.drain()
        assert good == ["ok"]
    finally:
        bus.shutdown()


def test_inmemory_bus_channels_isolated() -> None:
    bus = InMemoryBus()
    try:
        received: list[str] = []
        bus.subscribe("a", lambda _c, _p: received.append("a"))
        bus.subscribe("b", lambda _c, _p: received.append("b"))
        bus.publish("a", None)
        bus.publish("a", None)
        bus.publish("b", None)
        bus.drain()
        assert received.count("a") == 2
        assert received.count("b") == 1
    finally:
        bus.shutdown()


# =================================================================== CandleBuilder


def _ts(seconds_after_epoch: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds_after_epoch)


def test_candle_first_tick_no_close() -> None:
    cb = CandleBuilder(timeframe_seconds=60)
    assert cb.update(100.0, 1.0, _ts(0)) is None


def test_candle_same_bar_accumulates() -> None:
    cb = CandleBuilder(timeframe_seconds=60)
    cb.update(100.0, 1.0, _ts(0))
    cb.update(105.0, 2.0, _ts(15))
    cb.update(99.0, 3.0, _ts(45))
    partial = cb.current_partial()
    assert partial is not None
    assert partial.bar.open == 100.0
    assert partial.bar.high == 105.0
    assert partial.bar.low == 99.0
    assert partial.bar.close == 99.0
    assert partial.bar.volume == pytest.approx(6.0)


def test_candle_rolls_at_boundary() -> None:
    cb = CandleBuilder(timeframe_seconds=60)
    cb.update(100.0, 1.0, _ts(0))
    cb.update(110.0, 1.0, _ts(30))
    closed = cb.update(105.0, 1.0, _ts(60))
    assert closed is not None
    assert closed.bar.open == 100.0
    assert closed.bar.high == 110.0
    assert closed.bar.low == 100.0
    assert closed.bar.close == 110.0
    assert closed.bar_start == _ts(0)
    assert closed.bar_end == _ts(60)
    # next bar opened with the 105 tick
    cb.update(106.0, 1.0, _ts(75))
    partial = cb.current_partial()
    assert partial is not None
    assert partial.bar.open == 105.0


def test_candle_alignment_independent_of_first_tick() -> None:
    """A 60s bar boundary lands on minute boundaries, not on the first tick."""
    cb1 = CandleBuilder(timeframe_seconds=60)
    cb2 = CandleBuilder(timeframe_seconds=60)
    # builder 1 sees first tick at +5s
    cb1.update(100.0, 1.0, _ts(5))
    # builder 2 sees first tick at +20s — same minute, same bar_start
    cb2.update(100.0, 1.0, _ts(20))
    assert cb1.current_partial().bar_start == cb2.current_partial().bar_start  # type: ignore[union-attr]


def test_candle_force_close() -> None:
    cb = CandleBuilder(timeframe_seconds=60)
    cb.update(100.0, 1.0, _ts(10))
    closed = cb.force_close()
    assert closed is not None
    assert closed.bar.open == 100.0
    assert cb.current_partial() is None


def test_candle_gap_closes_previous_bar() -> None:
    cb = CandleBuilder(timeframe_seconds=60)
    cb.update(100.0, 1.0, _ts(0))
    # huge gap — skip several bars
    closed = cb.update(200.0, 1.0, _ts(600))
    assert closed is not None
    assert closed.bar_start == _ts(0)
    assert closed.bar.close == 100.0


# ====================================================================== NewsPoller


class _BiasedScorer(NullScorer):
    """positive on 'BUY', negative on 'AVOID'."""

    def score(self, text: str) -> SentimentScore:
        t = text.lower()
        if "buy" in t:
            return SentimentScore(label="positive", pos=0.95, neu=0.03, neg=0.02)
        if "avoid" in t:
            return SentimentScore(label="negative", pos=0.02, neu=0.03, neg=0.95)
        return super().score(text)

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        return [self.score(t) for t in texts]


def _news(title: str, link: str = "https://x/y") -> NewsItem:
    return NewsItem(
        ticker="HDFCBANK",
        title=title,
        link=link,
        published=datetime.now(timezone.utc),
        source="example",
    )


def test_news_poller_emits_alert_above_threshold() -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("Analyst says BUY now", link="link1")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    bus = InMemoryBus()
    alerts: list[dict] = []
    bus.subscribe("news.alert", lambda _c, p: alerts.append(p))

    try:
        poller = NewsPoller(
            feed=feed, bus=bus, research_log=rl,
            scorer=_BiasedScorer(), tickers=("HDFCBANK",),
            alert_threshold=0.70,
        )
        poller.run_once()
        bus.drain()
        assert len(alerts) == 1
        assert alerts[0]["urgency"] == "HIGH"
        # research log got the headline regardless
        rec = rl.latest("HDFCBANK", "news_headline")
        assert rec is not None and rec.value > 0.5
    finally:
        bus.shutdown()


def test_news_poller_deduplicates() -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("Strong BUY rating", link="dupe-link")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    bus = InMemoryBus()
    alerts: list[dict] = []
    bus.subscribe("news.alert", lambda _c, p: alerts.append(p))

    try:
        poller = NewsPoller(
            feed=feed, bus=bus, research_log=rl,
            scorer=_BiasedScorer(), tickers=("HDFCBANK",),
            alert_threshold=0.70,
        )
        poller.run_once()
        poller.run_once()
        poller.run_once()
        bus.drain()
        assert len(alerts) == 1
    finally:
        bus.shutdown()


def test_news_poller_neutral_no_alert() -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("Routine market update", link="neutral-1")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    bus = InMemoryBus()
    alerts: list[dict] = []
    bus.subscribe("news.alert", lambda _c, p: alerts.append(p))

    try:
        poller = NewsPoller(
            feed=feed, bus=bus, research_log=rl,
            scorer=_BiasedScorer(), tickers=("HDFCBANK",),
            alert_threshold=0.70,
        )
        poller.run_once()
        bus.drain()
        assert alerts == []
        # raw stream still has the article
        rec = rl.latest("HDFCBANK", "news_headline")
        assert rec is not None
    finally:
        bus.shutdown()


def test_news_poller_negative_signal_alerts() -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("Analysts AVOID the stock", link="avoid-1")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    bus = InMemoryBus()
    alerts: list[dict] = []
    bus.subscribe("news.alert", lambda _c, p: alerts.append(p))

    try:
        poller = NewsPoller(
            feed=feed, bus=bus, research_log=rl,
            scorer=_BiasedScorer(), tickers=("HDFCBANK",),
            alert_threshold=0.70,
        )
        poller.run_once()
        bus.drain()
        assert len(alerts) == 1
        assert alerts[0]["score"] < -0.5
    finally:
        bus.shutdown()
