"""Agent 1 — Indian-stock research, daily-cadence knowledge accumulation.

On every tick (every 15 min during market hours, hourly outside), for each
ticker in the configured universe:
  1. Fetch up to N most recent Google News headlines.
  2. Score each headline with the configured Scorer (FinBERT in prod,
     NullScorer when transformers unavailable).
  3. Aggregate to a single per-ticker `sentiment_score` and write a
     SignalRecord with the headlines as payload.
  4. Also writes a `last_close` price record when yfinance returns one.

The trading_sentiment agent later reads these records via
`research_log.recent(signal_type="sentiment_score", ...)` and triggers on
the threshold rules in `StrategyParams`.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_india import IndiaFeed
from models.finbert_scorer import NullScorer, Scorer
from record.research_log import ResearchLog, WriteSignal

log = logging.getLogger(__name__)

# Small starter universe — expandable from config later.
DEFAULT_INDIA_UNIVERSE: tuple[str, ...] = (
    "HDFCBANK",
    "ICICIBANK",
    "RELIANCE",
    "INFY",
    "TCS",
    "BAJFINANCE",
)


class ResearchIndia(Agent):
    name = "research_india"
    cadence = AgentCadence(every=timedelta(minutes=15))

    def __init__(
        self,
        feed: IndiaFeed,
        research_log: ResearchLog,
        scorer: Scorer | None = None,
        universe: tuple[str, ...] = DEFAULT_INDIA_UNIVERSE,
        news_limit: int = 10,
    ) -> None:
        self.feed = feed
        self.research_log = research_log
        self.scorer: Scorer = scorer or NullScorer()
        self.universe = universe
        self.news_limit = news_limit
        self.settings = get_settings()

    def run_once(self) -> None:
        signals: list[WriteSignal] = []
        for ticker in self.universe:
            try:
                items = self.feed.fetch_news(ticker, limit=self.news_limit)
            except Exception:
                log.exception("research_india news fetch failed for %s", ticker)
                items = []

            if items:
                scores = self.scorer.score_batch([n.title for n in items])
                avg = sum(s.signed for s in scores) / len(scores)
                signals.append(WriteSignal(
                    agent=self.name,
                    market="india",
                    ticker=ticker,
                    signal_type="sentiment_score",
                    value=avg,
                    payload={
                        "headline_count": len(items),
                        "headlines": [
                            {
                                "title": n.title,
                                "link": n.link,
                                "source": n.source,
                                "published": n.published.isoformat(),
                                "score": s.signed,
                                "label": s.label,
                            }
                            for n, s in zip(items, scores)
                        ],
                    },
                ))

            try:
                price = self.feed.fetch_latest_close(ticker)
            except Exception:
                log.exception("research_india price fetch failed for %s", ticker)
                price = None
            if price is not None:
                signals.append(WriteSignal(
                    agent=self.name,
                    market="india",
                    ticker=ticker,
                    signal_type="last_close",
                    value=price.close,
                    payload={
                        "ts": price.ts.isoformat(),
                        "volume": price.volume,
                    },
                ))

        if not signals:
            log.warning("research_india produced no signals this tick")
            return

        self.research_log.write_batch(signals)
        log.info("research_india wrote %d signals across %d tickers", len(signals), len(self.universe))
