"""News-speed agent.

Polls the news feed every `NewsPoller.poll_seconds` (default 30s), dedupes
by article link, scores each new headline with the configured Scorer, and:

  * Writes every fresh article to the research_log (sentiment_score type).
  * Publishes `news.alert` on the SignalBus when |score| exceeds the
    configured threshold.
  * Publishes `news.raw` for every fresh article (lower-rate stream that
    a dashboard can subscribe to without filtering).

Designed to run alongside `research_india` — the latter aggregates per-bar
sentiment for trading_sentiment; this one is the *fast lane* for breaking
news that needs to interrupt the slow loop.

Lives at *news speed* (~30s) — see the three-speed system in the project
memory. Trading agents subscribe to `news.alert` to react instantly.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_india import IndiaFeed, NewsItem
from infra.signal_bus import SignalBus
from models.finbert_scorer import NullScorer, Scorer
from record.research_log import ResearchLog, WriteSignal

log = logging.getLogger(__name__)


class NewsPoller(Agent):
    """Either runs in-thread (call run_once on the scheduler) or as a long-
    lived background loop via start()/stop(). Most deployments will use
    the start() path so the 30s cadence is independent of the main scheduler.
    """

    name = "news_poller"
    cadence = AgentCadence(every=timedelta(seconds=30))

    def __init__(
        self,
        *,
        feed: IndiaFeed,
        bus: SignalBus,
        research_log: ResearchLog,
        scorer: Scorer | None = None,
        tickers: tuple[str, ...] | None = None,
        alert_threshold: float = 0.70,
        poll_seconds: float = 30.0,
        per_ticker_limit: int = 5,
    ) -> None:
        self.feed = feed
        self.bus = bus
        self.research_log = research_log
        self.scorer: Scorer = scorer or NullScorer()
        self.tickers = tickers or get_settings().strategy.sentiment_universe
        self.alert_threshold = alert_threshold
        self.poll_seconds = poll_seconds
        self.per_ticker_limit = per_ticker_limit
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="news-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def run_once(self) -> None:
        """One pass over the universe. Safe to call from the scheduler too."""
        for ticker in self.tickers:
            try:
                self._poll_ticker(ticker)
            except Exception:
                log.exception("news_poller failed on %s", ticker)

    # ------------------------------------------------------------------ internals

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.poll_seconds)

    def _poll_ticker(self, ticker: str) -> None:
        items = self.feed.fetch_news(ticker, limit=self.per_ticker_limit)
        fresh = [n for n in items if n.link and n.link not in self._seen]
        if not fresh:
            return
        scores = self.scorer.score_batch([n.title for n in fresh])
        for item, score in zip(fresh, scores, strict=False):
            self._seen.add(item.link)
            payload = _serialize(item, score)
            self.research_log.write(WriteSignal(
                agent=self.name,
                market="india",
                ticker=ticker,
                signal_type="news_headline",
                value=score.signed,
                payload=payload,
            ))
            self.bus.publish("news.raw", payload)
            if abs(score.signed) >= self.alert_threshold:
                payload_alert = {**payload, "urgency": "HIGH"}
                self.bus.publish("news.alert", payload_alert)
                log.info(
                    "news.alert ticker=%s score=%.2f title=%s",
                    ticker, score.signed, item.title,
                )


def _serialize(item: NewsItem, score) -> dict[str, Any]:
    return {
        **asdict(item),
        "published": item.published.isoformat(),
        "score": score.signed,
        "label": score.label,
        "pos": score.pos,
        "neg": score.neg,
        "neu": score.neu,
        "ts": time.time(),
    }
