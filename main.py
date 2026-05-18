"""AlphaGrid entry point.

Wires APScheduler, registers each enabled agent at its cadence, and holds
the process open. Paper-mode is on by default (see .env.example).

Research agents have real implementations: every tick they fetch live data
and append to the research log. Trading agents are still stubs until their
build-plan slot.
"""
from __future__ import annotations

import logging
import signal
import sys
from typing import Iterable

from apscheduler.schedulers.blocking import BlockingScheduler

from agents.base import Agent
from agents.research_crypto import ResearchCrypto
from agents.research_india import ResearchIndia
from agents.trading_crypto_sent import TradingCryptoSent
from agents.trading_funding import TradingFunding
from agents.trading_momentum import TradingMomentum
from agents.trading_pairs import TradingPairs
from agents.trading_sentiment import TradingSentiment
from agents.trading_trend import TradingTrend
from config.settings import get_settings
from data.feeds_crypto import BinanceFeed
from data.feeds_india import GoogleNewsAndYFinanceFeed
from models.finbert_scorer import FinBertScorer, NullScorer, Scorer
from record.research_log import ResearchLog

log = logging.getLogger("alphagrid")


def _make_scorer() -> Scorer:
    """Try FinBERT; if transformers/torch missing, fall back to NullScorer.
    Either way, research_india keeps writing records on its cadence.
    """
    try:
        scorer = FinBertScorer()
        # Force a load attempt so we can fall back if the model is missing.
        scorer._ensure_loaded()  # noqa: SLF001
        log.info("FinBERT loaded")
        return scorer
    except Exception as exc:
        log.warning("FinBERT unavailable (%s); using NullScorer", exc)
        return NullScorer()


def _enabled_agents() -> Iterable[Agent]:
    settings = get_settings()
    toggles = settings.agents

    research_log = ResearchLog()

    if toggles.enable_research_india:
        yield ResearchIndia(
            feed=GoogleNewsAndYFinanceFeed(),
            research_log=research_log,
            scorer=_make_scorer(),
        )

    if toggles.enable_research_crypto:
        yield ResearchCrypto(
            feed=BinanceFeed(testnet=settings.binance.testnet),
            research_log=research_log,
        )

    # Trading agents are stubs until their build-plan week — they raise
    # NotImplementedError on first tick and the scheduler logs and moves on.
    if toggles.enable_trading_momentum:
        yield TradingMomentum()
    if toggles.enable_trading_sentiment:
        yield TradingSentiment()
    if toggles.enable_trading_pairs:
        yield TradingPairs()
    if toggles.enable_trading_funding:
        yield TradingFunding()
    if toggles.enable_trading_trend:
        yield TradingTrend()
    if toggles.enable_trading_crypto_sent:
        yield TradingCryptoSent()


def _safe_run(agent: Agent) -> None:
    try:
        agent.run_once()
    except NotImplementedError as exc:
        log.info("[%s] not implemented yet: %s", agent.name, exc)
    except Exception:
        log.exception("[%s] tick failed", agent.name)


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.runtime.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("alphagrid starting; paper_mode=%s", settings.runtime.paper_mode)

    scheduler = BlockingScheduler(timezone="UTC")
    for agent in _enabled_agents():
        interval = agent.cadence.every.total_seconds()
        # next_run_time=now triggers an immediate first tick so research
        # starts accumulating data without waiting a full cadence period.
        scheduler.add_job(
            _safe_run,
            "interval",
            seconds=interval,
            args=[agent],
            id=agent.name,
            replace_existing=True,
            max_instances=1,
        )
        log.info("registered %s every %ss", agent.name, interval)

    def _shutdown(_signum, _frame) -> None:
        log.info("shutdown signal received")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
