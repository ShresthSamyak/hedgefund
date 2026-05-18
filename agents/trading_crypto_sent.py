"""Agent 8 — Crypto sentiment / regime gate.

This agent does NOT place trades. It reads on-chain + sentiment signals
from research_log and writes a single `crypto_size_modifier` record per
tick. The funding-arb and trend agents read that modifier when sizing
their own trades.

Inputs (all optional — gate tolerates missing data and falls back to 0):
  * `regime` from research_crypto (risk_on / risk_off / neutral)
  * MVRV from research_crypto (when Glassnode credentials are set)
  * social_sentiment from research_crypto (when Reddit/X scraper is wired)

Output:
  SignalRecord(signal_type="crypto_size_modifier",
               ticker="PORTFOLIO",
               value=modifier  # in [-1, +1]
               payload={"components": {...}, "explanation": "..."})

Modifier semantics (read by trading_funding and trading_trend):
  +1.0 -> scale notional by (1 + crypto_sent_size_modifier) = +20%
   0.0 -> neutral
  -1.0 -> scale notional by (1 - crypto_sent_size_modifier) = -20%
"""
from __future__ import annotations

import logging
from datetime import timedelta

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from record.research_log import ResearchLog, WriteSignal

log = logging.getLogger(__name__)


class TradingCryptoSent(Agent):
    name = "trading_crypto_sent"
    cadence = AgentCadence(every=timedelta(hours=4))

    def __init__(self, *, research_log: ResearchLog) -> None:
        self.research_log = research_log
        self.settings = get_settings()

    def run_once(self) -> None:
        sp = self.settings.strategy

        # 1) Regime contribution (-1, 0, +1).
        regime_rec = self.research_log.latest("PORTFOLIO", "regime")
        regime_score = regime_rec.value if regime_rec is not None else 0.0

        # 2) MVRV contribution. <1 -> bullish (+1), >3.5 -> bearish (-1).
        mvrv_score = 0.0
        mvrv_rec = self.research_log.latest("PORTFOLIO", "mvrv")
        if mvrv_rec is not None:
            mvrv_score = _mvrv_to_score(
                mvrv_rec.value, sp.crypto_sent_mvrv_bullish, sp.crypto_sent_mvrv_bearish
            )

        # 3) Social sentiment contribution (already in [-1, +1] roughly).
        social_score = 0.0
        social_rec = self.research_log.latest("PORTFOLIO", "social_sentiment")
        if social_rec is not None:
            social_score = max(-1.0, min(1.0, social_rec.value))

        # Aggregate available components — average over those present so a
        # missing data source doesn't dilute the rest toward zero.
        components = {
            "regime": regime_score if regime_rec is not None else None,
            "mvrv": mvrv_score if mvrv_rec is not None else None,
            "social": social_score if social_rec is not None else None,
        }
        present = [v for v in components.values() if v is not None]
        modifier = sum(present) / len(present) if present else 0.0
        modifier = max(-1.0, min(1.0, modifier))

        explanation = _explain(components, modifier)
        self.research_log.write(WriteSignal(
            agent=self.name,
            market="crypto",
            ticker="PORTFOLIO",
            signal_type="crypto_size_modifier",
            value=modifier,
            payload={
                "components": components,
                "explanation": explanation,
                "lookback_window_hours": sp.crypto_sent_lookback_hours,
            },
        ))
        log.info("crypto_size_modifier=%+.3f (%s)", modifier, explanation)


def _mvrv_to_score(mvrv: float, bullish_th: float, bearish_th: float) -> float:
    """Linear-ish mapping. Below bullish_th -> +1; above bearish_th -> -1;
    in between, linear interpolation.
    """
    if mvrv <= bullish_th:
        return 1.0
    if mvrv >= bearish_th:
        return -1.0
    # Map [bullish_th, bearish_th] linearly to [+1, -1].
    span = bearish_th - bullish_th
    if span <= 0:
        return 0.0
    return 1.0 - 2.0 * (mvrv - bullish_th) / span


def _explain(components: dict, modifier: float) -> str:
    parts = []
    for k, v in components.items():
        if v is None:
            parts.append(f"{k}=?")
        else:
            parts.append(f"{k}={v:+.2f}")
    return f"modifier {modifier:+.2f}; {', '.join(parts)}"
