"""LLM reasoning layer.

FinBERT scores headlines numerically (cheap, local, ~35-45 ms/headline).
This module adds the *reasoning* layer on top — one-sentence trade-impact
summaries, regime narratives, dashboard captions — using Vertex AI's
Gemini / Claude / Llama models.

Three tiers map task → model:
  fast        Gemini 3.1 Flash Lite     routine, high-frequency  (~$0.075/M tokens)
  reasoning   Gemini 3.1 Pro            critical decisions       (~$1.25/M tokens)
  coding      Claude Sonnet 4.6         code or agent tasks      (~$3/M tokens)

If no `VERTEX_API_KEY` is set and ADC isn't available, the factory hands
back a `NullLLM` so callers can run unchanged in paper / test mode.

Cost tracking: every call increments per-tier counters; the
`get_cost_summary()` snapshot is what the daily snapshot reports include.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from config.settings import get_settings

log = logging.getLogger("alphagrid.llm")

TIERS = ("fast", "reasoning", "coding")


# ---------------------------------------------------------------- result types


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: str
    in_tokens: int = 0
    out_tokens: int = 0


@dataclass
class CostCounter:
    calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0


@dataclass
class CostSummary:
    by_tier: dict[str, CostCounter] = field(default_factory=dict)

    def add(self, resp: LLMResponse) -> None:
        c = self.by_tier.setdefault(resp.tier, CostCounter())
        c.calls += 1
        c.in_tokens += resp.in_tokens
        c.out_tokens += resp.out_tokens

    def total_tokens(self) -> int:
        return sum(c.in_tokens + c.out_tokens for c in self.by_tier.values())


# ---------------------------------------------------------------- protocol


class LLMClient(Protocol):
    def complete(self, prompt: str, *, tier: str = "fast") -> LLMResponse: ...


# ---------------------------------------------------------------- NullLLM


class NullLLM:
    """Returns a canned response. Used when no API key is configured
    (paper-mode burn-in, tests, CI). Lets every caller stay unchanged.
    """

    def __init__(self, canned: str = "(LLM disabled)") -> None:
        self._canned = canned
        self.cost = CostSummary()

    def complete(self, prompt: str, *, tier: str = "fast") -> LLMResponse:
        resp = LLMResponse(
            text=self._canned, model="null", tier=tier,
            in_tokens=len(prompt) // 4,    # rough estimate; null doesn't bill
            out_tokens=0,
        )
        self.cost.add(resp)
        return resp


# ---------------------------------------------------------------- VertexAIClient


class VertexAIClient:
    """Wraps `google-genai` (the new unified SDK). Lazy-imports it so the
    module loads cleanly without the dependency installed.

    Auth: prefers api_key when set, falls back to Application Default
    Credentials (ADC) otherwise — works on the Azure VM after `gcloud
    auth application-default login`.
    """

    def __init__(self) -> None:
        try:
            from google import genai  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is required for VertexAIClient; "
                "`pip install google-genai`"
            ) from exc
        self.settings = get_settings().vertex
        self._client: Any = self._build_client()
        self.cost = CostSummary()

    def _build_client(self):
        from google import genai
        if self.settings.api_key:
            log.info("vertex client authenticating via API key")
            return genai.Client(api_key=self.settings.api_key)
        log.info("vertex client authenticating via Application Default Credentials")
        if not self.settings.project:
            raise RuntimeError(
                "VERTEX_PROJECT must be set when using ADC auth (no api key)"
            )
        return genai.Client(
            vertexai=True,
            project=self.settings.project,
            location=self.settings.location,
        )

    def complete(self, prompt: str, *, tier: str = "fast") -> LLMResponse:
        model = self._model_for(tier)
        try:
            raw = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config={"max_output_tokens": self.settings.max_output_tokens},
            )
        except Exception as exc:
            log.exception("vertex completion failed at tier=%s", tier)
            raise RuntimeError(f"vertex completion failed: {exc}") from exc

        text = self._extract_text(raw)
        in_tokens, out_tokens = self._extract_token_counts(raw)
        resp = LLMResponse(
            text=text, model=model, tier=tier,
            in_tokens=in_tokens, out_tokens=out_tokens,
        )
        self.cost.add(resp)
        return resp

    # ---- helpers ----

    def _model_for(self, tier: str) -> str:
        if tier == "fast":
            return self.settings.fast_model
        if tier == "reasoning":
            return self.settings.reasoning_model
        if tier == "coding":
            return self.settings.coding_model
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")

    @staticmethod
    def _extract_text(raw: Any) -> str:
        # The SDK exposes a convenience `.text`; fall back to walking
        # candidates if the shape changes between versions.
        text = getattr(raw, "text", None)
        if isinstance(text, str):
            return text.strip()
        candidates = getattr(raw, "candidates", None) or []
        for c in candidates:
            content = getattr(c, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if parts:
                joined = "".join(getattr(p, "text", "") for p in parts)
                if joined:
                    return joined.strip()
        return ""

    @staticmethod
    def _extract_token_counts(raw: Any) -> tuple[int, int]:
        usage = getattr(raw, "usage_metadata", None)
        if usage is None:
            return 0, 0
        return (
            int(getattr(usage, "prompt_token_count", 0) or 0),
            int(getattr(usage, "candidates_token_count", 0) or 0),
        )


# ---------------------------------------------------------------- factory


def build_llm_client() -> LLMClient:
    """Picks the best available client based on environment.

    Order:
      1. VertexAIClient if google-genai is installed AND (api_key OR ADC).
      2. NullLLM otherwise.
    """
    settings = get_settings().vertex
    if not settings.api_key and not settings.project:
        log.info("vertex not configured (no api_key, no project); using NullLLM")
        return NullLLM()
    try:
        return VertexAIClient()
    except Exception as exc:
        log.warning("VertexAIClient unavailable (%s); using NullLLM", exc)
        return NullLLM()
