"""Agent 2 — Crypto research, daily-cadence knowledge accumulation.

On every tick (every 8h, aligned with Binance funding cycles), this agent:
  1. Pulls the current funding rate for each symbol in the universe.
  2. Writes one `funding_rate` SignalRecord per symbol.
  3. Computes a portfolio-wide regime (risk_on / risk_off / neutral) from
     the cross-section of funding rates and writes one `regime` record.

The research log is append-only, so this becomes a daily-growing dataset
that trading agents read with `recent(window=...)` calls.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_crypto import CryptoFeed
from record.research_log import ResearchLog, WriteSignal

log = logging.getLogger(__name__)


class ResearchCrypto(Agent):
    name = "research_crypto"
    cadence = AgentCadence(every=timedelta(hours=8), aligned_to="binance_funding_8h")

    def __init__(self, feed: CryptoFeed, research_log: ResearchLog) -> None:
        self.feed = feed
        self.research_log = research_log
        self.settings = get_settings()

    def run_once(self) -> None:
        universe = self.settings.strategy.funding_universe
        rates: list[float] = []
        signals: list[WriteSignal] = []

        for symbol in universe:
            try:
                pt = self.feed.fetch_funding_rate(symbol)
            except KeyError:
                # Expected at the very start of a backtest window — no
                # funding history visible yet under the no-future-leakage
                # rule. Quietly skip; the next tick will have data.
                log.debug("research_crypto: no funding data yet for %s — skipping tick", symbol)
                continue
            except Exception:
                log.exception("research_crypto fetch failed for %s — skipping tick", symbol)
                continue
            rates.append(pt.rate)
            signals.append(WriteSignal(
                agent=self.name,
                market="crypto",
                ticker=symbol,
                signal_type="funding_rate",
                value=pt.rate,
                payload={
                    "mark_price": pt.mark_price,
                    "funding_time": pt.funding_time.isoformat(),
                },
            ))

        if not signals:
            log.warning("research_crypto produced no signals this tick")
            return

        regime = classify_regime(rates)
        signals.append(WriteSignal(
            agent=self.name,
            market="crypto",
            ticker="PORTFOLIO",
            signal_type="regime",
            value=_regime_to_score(regime),
            payload={"regime": regime, "universe_rates": rates},
        ))

        ids = self.research_log.write_batch(signals)
        log.info(
            "research_crypto wrote %d signals, regime=%s, rates=%s",
            len(ids), regime, [round(r, 5) for r in rates],
        )


def classify_regime(rates: list[float]) -> str:
    """Risk-off when funding is broadly negative (longs being paid -> bearish positioning).
    Risk-on when broadly positive and elevated. Neutral otherwise.

    Heuristic — refine after a month of data.
    """
    if not rates:
        return "neutral"
    avg = sum(rates) / len(rates)
    if avg <= -0.0002:
        return "risk_off"
    if avg >= 0.0005:
        return "risk_on"
    return "neutral"


def _regime_to_score(regime: str) -> float:
    return {"risk_on": 1.0, "risk_off": -1.0, "neutral": 0.0}[regime]
