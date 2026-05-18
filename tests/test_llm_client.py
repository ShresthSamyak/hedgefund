"""LLM client + research_india integration tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.research_india import ResearchIndia
from data.feeds_india import NewsItem, StaticIndiaFeed
from models.finbert_scorer import NullScorer, SentimentScore
from models.llm_client import (
    LLMResponse,
    NullLLM,
    build_llm_client,
)
from record.research_log import ResearchLog


# ----------------------------------------------------------------- NullLLM


def test_null_llm_returns_canned() -> None:
    llm = NullLLM(canned="ok")
    resp = llm.complete("anything", tier="fast")
    assert resp.text == "ok"
    assert resp.model == "null"
    assert resp.tier == "fast"
    assert llm.cost.by_tier["fast"].calls == 1


def test_null_llm_tracks_per_tier_counts() -> None:
    llm = NullLLM()
    llm.complete("p1", tier="fast")
    llm.complete("p2", tier="fast")
    llm.complete("p3", tier="reasoning")
    assert llm.cost.by_tier["fast"].calls == 2
    assert llm.cost.by_tier["reasoning"].calls == 1


# ----------------------------------------------------------------- factory


def test_build_llm_client_returns_null_when_not_configured(monkeypatch) -> None:
    """No api_key and no project → NullLLM, never raises."""
    from config.settings import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings.vertex, "api_key", "", raising=True)
    monkeypatch.setattr(settings.vertex, "project", "", raising=True)
    client = build_llm_client()
    assert isinstance(client, NullLLM)


# ----------------------------------------------------------------- biased scorer


class _BiasedScorer(NullScorer):
    """Returns +0.9 for 'beat' headlines, -0.9 for 'miss', neutral otherwise."""

    def score(self, text: str) -> SentimentScore:
        t = text.lower()
        if "beat" in t:
            return SentimentScore(label="positive", pos=0.9, neu=0.05, neg=0.05)
        if "miss" in t:
            return SentimentScore(label="negative", pos=0.05, neu=0.05, neg=0.9)
        return super().score(text)

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        return [self.score(t) for t in texts]


class _RecordingLLM:
    """Captures every prompt + returns a stamp so tests can check routing."""

    def __init__(self, response: str = "POSITIVE|0.85|earnings beat suggests upside") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []   # (tier, prompt)

    def complete(self, prompt: str, *, tier: str = "fast") -> LLMResponse:
        self.calls.append((tier, prompt))
        return LLMResponse(
            text=self.response, model="recording", tier=tier,
            in_tokens=len(prompt) // 4, out_tokens=len(self.response) // 4,
        )


def _news(ticker: str, title: str, link: str) -> NewsItem:
    return NewsItem(
        ticker=ticker, title=title, link=link,
        published=datetime.now(timezone.utc), source="ex",
    )


# ----------------------------------------------------------------- integration


def test_research_india_attaches_summary_when_threshold_crossed(monkeypatch) -> None:
    """Strong positive signal (|avg| >= 0.7) triggers the LLM call and the
    summary lands in the SignalRecord payload.
    """
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [
        _news("HDFCBANK", "HDFCBANK beat earnings expectations", "L1"),
        _news("HDFCBANK", "HDFCBANK beat guidance", "L2"),
    ])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    llm = _RecordingLLM(response="POSITIVE|0.85|earnings beat suggests upside")

    agent = ResearchIndia(
        feed=feed, research_log=rl,
        scorer=_BiasedScorer(), llm=llm,
        universe=("HDFCBANK",),
    )
    monkeypatch.setattr(agent.settings.vertex, "enable_llm_summaries", True)

    agent.run_once()
    rec = rl.latest("HDFCBANK", "sentiment_score")
    assert rec is not None
    assert rec.value > 0.5
    assert rec.payload.get("llm_summary", "").startswith("POSITIVE|")
    # Should have hit the LLM exactly once at the fast tier.
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "fast"
    assert "HDFCBANK" in llm.calls[0][1]


def test_research_india_skips_llm_when_disabled(monkeypatch) -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("HDFCBANK", "HDFCBANK beat earnings", "L1")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    llm = _RecordingLLM()

    agent = ResearchIndia(
        feed=feed, research_log=rl,
        scorer=_BiasedScorer(), llm=llm,
        universe=("HDFCBANK",),
    )
    monkeypatch.setattr(agent.settings.vertex, "enable_llm_summaries", False)

    agent.run_once()
    rec = rl.latest("HDFCBANK", "sentiment_score")
    assert rec is not None
    assert "llm_summary" not in rec.payload
    assert llm.calls == []


def test_research_india_skips_llm_below_threshold(monkeypatch) -> None:
    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [_news("HDFCBANK", "HDFCBANK announces routine update", "L1")])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    llm = _RecordingLLM()

    agent = ResearchIndia(
        feed=feed, research_log=rl,
        scorer=NullScorer(),   # neutral score = 0.0
        llm=llm, universe=("HDFCBANK",),
    )
    monkeypatch.setattr(agent.settings.vertex, "enable_llm_summaries", True)

    agent.run_once()
    rec = rl.latest("HDFCBANK", "sentiment_score")
    # Neutral signal: payload exists but no llm_summary because |avg| < 0.7.
    assert rec is not None
    assert "llm_summary" not in rec.payload
    assert llm.calls == []


def test_research_india_swallows_llm_errors(monkeypatch) -> None:
    """An LLM failure must not break the research tick — sentiment still recorded."""

    class _BrokenLLM:
        def complete(self, prompt: str, *, tier: str = "fast") -> LLMResponse:
            raise RuntimeError("vertex 500")

    feed = StaticIndiaFeed()
    feed.set_news("HDFCBANK", [
        _news("HDFCBANK", "HDFCBANK beat earnings", "L1"),
        _news("HDFCBANK", "HDFCBANK beat guidance", "L2"),
    ])
    rl = ResearchLog(db_url="sqlite:///:memory:")
    agent = ResearchIndia(
        feed=feed, research_log=rl,
        scorer=_BiasedScorer(), llm=_BrokenLLM(),
        universe=("HDFCBANK",),
    )
    monkeypatch.setattr(agent.settings.vertex, "enable_llm_summaries", True)

    agent.run_once()
    rec = rl.latest("HDFCBANK", "sentiment_score")
    assert rec is not None
    # Sentiment still recorded; summary omitted because the LLM failed.
    assert "llm_summary" not in rec.payload
