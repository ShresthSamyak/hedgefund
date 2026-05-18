"""Sentiment scorers for headlines.

Two implementations behind one Protocol:
  * `FinBertScorer` — lazy-loads ProsusAI/finbert via transformers. First
    call is slow (~10s + model download on first run, cached after). Best
    when torch+transformers are available.
  * `NullScorer` — neutral score, used when the model can't load. Lets
    research agents still write structured records on every tick.

Agents pick which one to use at construction time. The research log doesn't
care which scorer produced the value; it just stores it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentimentScore:
    label: str           # "positive" | "neutral" | "negative"
    pos: float           # probability mass on positive
    neu: float
    neg: float

    @property
    def signed(self) -> float:
        """Signed score in [-1, 1]: pos contribution minus neg contribution."""
        return self.pos - self.neg


class Scorer(Protocol):
    def score(self, text: str) -> SentimentScore: ...
    def score_batch(self, texts: list[str]) -> list[SentimentScore]: ...


class NullScorer:
    """Returns neutral 0 scores. Used when FinBERT can't load.

    Agents using this still produce records — value just stays at 0 until
    a real model is wired up. This keeps the daily-research cadence intact.
    """

    def score(self, text: str) -> SentimentScore:
        return SentimentScore(label="neutral", pos=0.0, neu=1.0, neg=0.0)

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        return [self.score(t) for t in texts]


class FinBertScorer:
    """ProsusAI/finbert wrapper. Lazy-loads on first call."""

    MODEL_NAME = "ProsusAI/finbert"

    def __init__(self) -> None:
        self._pipe = None

    def _ensure_loaded(self):
        if self._pipe is not None:
            return self._pipe
        try:
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )
        except ImportError as exc:
            raise RuntimeError(
                "transformers + torch are required for FinBertScorer"
            ) from exc
        log.info("loading FinBERT model %s (first call is slow)", self.MODEL_NAME)
        tok = AutoTokenizer.from_pretrained(self.MODEL_NAME)
        model = AutoModelForSequenceClassification.from_pretrained(self.MODEL_NAME)
        self._pipe = pipeline(
            "text-classification",
            model=model,
            tokenizer=tok,
            return_all_scores=True,
            truncation=True,
        )
        return self._pipe

    def score(self, text: str) -> SentimentScore:
        return self.score_batch([text])[0]

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        if not texts:
            return []
        pipe = self._ensure_loaded()
        results = cast(list[list[dict[str, Any]]], pipe(texts))
        out: list[SentimentScore] = []
        for row in results:
            mapping = {
                str(item["label"]).lower(): float(item["score"]) for item in row
            }
            pos = mapping.get("positive", 0.0)
            neu = mapping.get("neutral", 0.0)
            neg = mapping.get("negative", 0.0)
            label = max(("positive", pos), ("neutral", neu), ("negative", neg), key=lambda x: x[1])[0]
            out.append(SentimentScore(label=label, pos=pos, neu=neu, neg=neg))
        return out
