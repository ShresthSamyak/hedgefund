"""AlphaGrid entry point.

Wires APScheduler, registers each enabled agent at its cadence, and holds
the process open. Paper-mode is on by default (see .env.example).

Real implementations live now:
  * research_india    — every 15 min, accumulates news + sentiment + prices
  * research_crypto   — every 8h, accumulates funding rates + regime signal
  * trading_funding   — every 8h, reads research_log and proposes carry trades
  * trading_momentum  — every 5 min during IST session, EWMA cross on Nifty subset
  * trading_sentiment — every 15 min, decay-weighted FinBERT signal -> small longs

Other trading agents are stubs until their build-plan slot.
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
from comms.approval_gate import NullApprovalGate
from config.settings import get_settings
from data.feeds_crypto import BinanceFeed
from data.feeds_india import GoogleNewsAndYFinanceFeed
from execution.trade_router import TradeRouter
from models.finbert_scorer import FinBertScorer, NullScorer, Scorer
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from risk.risk_manager import RiskManager

log = logging.getLogger("alphagrid")


def _make_scorer() -> Scorer:
    """Try FinBERT; fall back to NullScorer if transformers/torch missing.
    Either way, research_india keeps writing records on its cadence.
    """
    try:
        scorer = FinBertScorer()
        scorer._ensure_loaded()  # noqa: SLF001
        log.info("FinBERT loaded")
        return scorer
    except Exception as exc:
        log.warning("FinBERT unavailable (%s); using NullScorer", exc)
        return NullScorer()


def _enabled_agents() -> Iterable[Agent]:
    settings = get_settings()
    toggles = settings.agents

    # Shared infrastructure — single instance reused across agents.
    research_log = ResearchLog()
    track_record = TrackRecord()
    risk_manager = RiskManager(track_record)
    approval_gate = NullApprovalGate()
    trade_router = TradeRouter(
        risk_manager=risk_manager,
        approval_gate=approval_gate,
        track_record=track_record,
    )

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

    if toggles.enable_trading_funding:
        yield TradingFunding(
            research_log=research_log,
            track_record=track_record,
            trade_router=trade_router,
        )

    # Indian-equities agents share the same feed object.
    india_feed = GoogleNewsAndYFinanceFeed() if (
        toggles.enable_trading_momentum or toggles.enable_trading_sentiment
    ) else None

    if toggles.enable_trading_momentum:
        assert india_feed is not None
        yield TradingMomentum(
            feed=india_feed,
            research_log=research_log,
            track_record=track_record,
            trade_router=trade_router,
        )

    if toggles.enable_trading_sentiment:
        assert india_feed is not None
        yield TradingSentiment(
            feed=india_feed,
            research_log=research_log,
            track_record=track_record,
            trade_router=trade_router,
        )

    # Stubs — raise NotImplementedError on their first tick; the scheduler
    # logs and moves on.
    if toggles.enable_trading_pairs:
        yield TradingPairs()
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
