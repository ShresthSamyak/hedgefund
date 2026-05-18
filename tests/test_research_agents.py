"""Research agents — end-to-end tests with fake feeds + real ResearchLog."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.research_crypto import ResearchCrypto, classify_regime
from agents.research_india import ResearchIndia
from data.feeds_crypto import FundingPoint, StaticCryptoFeed
from data.feeds_india import NewsItem, StaticIndiaFeed
from models.finbert_scorer import NullScorer, SentimentScore
from record.research_log import ResearchLog


@pytest.fixture
def rl() -> ResearchLog:
    return ResearchLog(db_url="sqlite:///:memory:")


# ----------------------------------------------------------- crypto research


def test_crypto_research_writes_one_record_per_symbol(rl: ResearchLog) -> None:
    feed = StaticCryptoFeed()
    feed.set("BTC/USDT", rate=0.012, mark_price=60_000.0)
    feed.set("ETH/USDT", rate=0.008, mark_price=3_000.0)

    agent = ResearchCrypto(feed=feed, research_log=rl)
    agent.run_once()

    btc = rl.latest("BTC/USDT", "funding_rate")
    eth = rl.latest("ETH/USDT", "funding_rate")
    assert btc is not None and btc.value == pytest.approx(0.012)
    assert eth is not None and eth.value == pytest.approx(0.008)


def test_crypto_research_writes_regime(rl: ResearchLog) -> None:
    feed = StaticCryptoFeed()
    feed.set("BTC/USDT", rate=0.001)
    feed.set("ETH/USDT", rate=0.001)

    agent = ResearchCrypto(feed=feed, research_log=rl)
    agent.run_once()
    regime = rl.latest("PORTFOLIO", "regime")
    assert regime is not None
    assert regime.payload["regime"] == "risk_on"


def test_classify_regime_thresholds() -> None:
    assert classify_regime([]) == "neutral"
    assert classify_regime([-0.0005, -0.0003]) == "risk_off"
    assert classify_regime([0.0008, 0.0006]) == "risk_on"
    assert classify_regime([0.0001, 0.0001]) == "neutral"


def test_crypto_research_survives_partial_fetch_failure(rl: ResearchLog) -> None:
    class HalfBrokenFeed(StaticCryptoFeed):
        def fetch_funding_rate(self, symbol: str) -> FundingPoint:
            if symbol == "ETH/USDT":
                raise RuntimeError("api 500")
            return super().fetch_funding_rate(symbol)

    feed = HalfBrokenFeed()
    feed.set("BTC/USDT", rate=0.012)
    feed.set("ETH/USDT", rate=0.0)

    agent = ResearchCrypto(feed=feed, research_log=rl)
    agent.run_once()
    # BTC written, ETH skipped, regime still computed from the one rate.
    assert rl.latest("BTC/USDT", "funding_rate") is not None
    assert rl.latest("ETH/USDT", "funding_rate") is None
    assert rl.latest("PORTFOLIO", "regime") is not None


def test_crypto_research_accumulates_over_ticks(rl: ResearchLog) -> None:
    feed = StaticCryptoFeed()
    agent = ResearchCrypto(feed=feed, research_log=rl)
    for i, rate in enumerate([0.010, 0.012, 0.014]):
        feed.set("BTC/USDT", rate=rate)
        agent.run_once()
    rows = rl.recent("funding_rate", window=timedelta(hours=1), ticker="BTC/USDT")
    assert len(rows) == 3
    assert sorted(r.value for r in rows) == [pytest.approx(0.010), pytest.approx(0.012), pytest.approx(0.014)]


# ----------------------------------------------------------- india research


def _news(ticker: str, title: str) -> NewsItem:
    return NewsItem(
        ticker=ticker,
        title=title,
        link=f"https://news.example/{ticker}",
        published=datetime.now(timezone.utc),
        source="example",
    )


class _TwoWayScorer(NullScorer):
    """Test scorer that says everything containing 'beat' is positive,
    everything containing 'miss' is negative."""

    def score(self, text: str) -> SentimentScore:
        t = text.lower()
        if "beat" in t:
            return SentimentScore(label="positive", pos=0.9, neu=0.05, neg=0.05)
        if "miss" in t:
            return SentimentScore(label="negative", pos=0.05, neu=0.05, neg=0.9)
        return super().score(text)

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        return [self.score(t) for t in texts]


def test_india_research_writes_sentiment_per_ticker(rl: ResearchLog) -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [
        _news("HDFCBANK", "HDFCBANK beat earnings estimates"),
        _news("HDFCBANK", "HDFCBANK beat guidance"),
    ])
    feed.set_news("INFY", [
        _news("INFY", "INFY miss on revenue"),
    ])

    agent = ResearchIndia(
        feed=feed,
        research_log=rl,
        scorer=_TwoWayScorer(),
        universe=("HDFCBANK", "INFY"),
    )
    agent.run_once()

    hdfc = rl.latest("HDFCBANK", "sentiment_score")
    infy = rl.latest("INFY", "sentiment_score")
    assert hdfc is not None and hdfc.value > 0.5
    assert infy is not None and infy.value < -0.5
    assert hdfc.payload["headline_count"] == 2


def test_india_research_handles_no_news(rl: ResearchLog) -> None:
    feed = StaticIndiaFeed()  # no news set
    agent = ResearchIndia(
        feed=feed,
        research_log=rl,
        scorer=NullScorer(),
        universe=("RELIANCE",),
    )
    agent.run_once()
    # No signals written (no news, no price).
    assert rl.latest("RELIANCE", "sentiment_score") is None


def test_india_research_writes_price_when_available(rl: ResearchLog) -> None:
    feed = StaticIndiaFeed()
    feed.set_price("RELIANCE", close=2_950.50, volume=12_345_678)
    feed.set_news("RELIANCE", [])

    agent = ResearchIndia(
        feed=feed,
        research_log=rl,
        scorer=NullScorer(),
        universe=("RELIANCE",),
    )
    agent.run_once()

    price = rl.latest("RELIANCE", "last_close")
    assert price is not None and price.value == pytest.approx(2_950.50)
