"""AlphaGrid entry point.

Wires the APScheduler, registers each enabled agent at its cadence, and
holds the process open. Paper-mode is on by default (see .env.example).

This file intentionally does not start trading anything yet — week 1
deliverable is plumbing only. Agents raise NotImplementedError until their
build-plan slot arrives.
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

log = logging.getLogger("alphagrid")


def _enabled_agents() -> Iterable[Agent]:
    toggles = get_settings().agents
    candidates: list[tuple[bool, Agent]] = [
        (toggles.enable_research_india, ResearchIndia()),
        (toggles.enable_research_crypto, ResearchCrypto()),
        (toggles.enable_trading_momentum, TradingMomentum()),
        (toggles.enable_trading_sentiment, TradingSentiment()),
        (toggles.enable_trading_pairs, TradingPairs()),
        (toggles.enable_trading_funding, TradingFunding()),
        (toggles.enable_trading_trend, TradingTrend()),
        (toggles.enable_trading_crypto_sent, TradingCryptoSent()),
    ]
    for enabled, agent in candidates:
        if enabled:
            yield agent


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
        scheduler.add_job(_safe_run, "interval", seconds=interval, args=[agent], id=agent.name)
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
