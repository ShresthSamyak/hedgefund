"""AlphaGrid entry point.

Boots the four-speed system:
  * Tick speed   — Binance WebSocket -> CandleBuilder -> SignalBus
  * News speed   — Google News RSS poller -> FinBERT -> SignalBus
  * Bar speed    — APScheduler -> 8 trading + research agents
  * Macro speed  — research_crypto + regime gate (8h+)

Paper-mode default. Telegram approval gate enabled when both
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from typing import Iterable

from apscheduler.schedulers.blocking import BlockingScheduler

from agents.base import Agent
from agents.news_poller import NewsPoller
from agents.research_crypto import ResearchCrypto
from agents.research_india import ResearchIndia
from agents.trading_crypto_sent import TradingCryptoSent
from agents.trading_funding import TradingFunding
from agents.trading_momentum import TradingMomentum
from agents.trading_pairs import TradingPairs
from agents.trading_sentiment import TradingSentiment
from agents.trading_trend import TradingTrend
from comms.approval_gate import ApprovalGate, NullApprovalGate
from config.settings import get_settings
from data.feeds_crypto import BinanceFeed
from data.feeds_india import GoogleNewsAndYFinanceFeed, IndiaFeed
from data.live_crypto_stream import BinanceWebSocketStream
from execution.trade_router import TradeRouter
from infra.signal_bus import InMemoryBus, SignalBus
from models.finbert_scorer import FinBertScorer, NullScorer, Scorer
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from risk.risk_manager import RiskManager

log = logging.getLogger("alphagrid")


# ---------------------------------------------------------------- infrastructure

class AppContext:
    """Container for everything an agent needs. Built once in main()."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.bus: SignalBus = InMemoryBus()
        self.research_log = ResearchLog()
        self.track_record = TrackRecord()
        self.risk_manager = RiskManager(self.track_record)
        self.approval_gate: ApprovalGate = _build_approval_gate(self.settings)
        self.trade_router = TradeRouter(
            risk_manager=self.risk_manager,
            approval_gate=self.approval_gate,
            track_record=self.track_record,
        )
        # Feeds — created lazily based on toggles.
        self._crypto_feed: BinanceFeed | None = None
        self._india_feed: IndiaFeed | None = None
        # Background workers — owned here so shutdown can stop them.
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_stream: BinanceWebSocketStream | None = None
        self._news_poller: NewsPoller | None = None

    # ---------- feed accessors

    def crypto_feed(self) -> BinanceFeed:
        if self._crypto_feed is None:
            self._crypto_feed = BinanceFeed(testnet=self.settings.binance.testnet)
        return self._crypto_feed

    def india_feed(self) -> IndiaFeed:
        if self._india_feed is None:
            self._india_feed = GoogleNewsAndYFinanceFeed()
        return self._india_feed

    # ---------- background workers

    def start_live_crypto_stream(self) -> None:
        toggles = self.settings.agents
        if not toggles.enable_live_crypto_stream:
            return
        stream = BinanceWebSocketStream(
            symbols=toggles.live_stream_symbols,
            bus=self.bus,
            timeframe_seconds=toggles.live_stream_timeframe_seconds,
            futures=toggles.live_stream_use_futures,
        )
        self._ws_stream = stream

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop
            try:
                loop.run_until_complete(stream.start())
                loop.run_forever()
            finally:
                loop.close()

        self._ws_thread = threading.Thread(target=_run_loop, daemon=True, name="live-stream")
        self._ws_thread.start()
        log.info(
            "live crypto stream started: %s, %ss bars, futures=%s",
            toggles.live_stream_symbols,
            toggles.live_stream_timeframe_seconds,
            toggles.live_stream_use_futures,
        )

    def start_news_poller(self, scorer: Scorer) -> None:
        toggles = self.settings.agents
        if not toggles.enable_news_poller:
            return
        poller = NewsPoller(
            feed=self.india_feed(),
            bus=self.bus,
            research_log=self.research_log,
            scorer=scorer,
            alert_threshold=toggles.news_poller_alert_threshold,
            poll_seconds=toggles.news_poller_poll_seconds,
        )
        poller.start()
        self._news_poller = poller
        log.info(
            "news poller started: %d tickers, every %.0fs, alert>=%.2f",
            len(poller.tickers), poller.poll_seconds, poller.alert_threshold,
        )

    def shutdown(self) -> None:
        if self._news_poller is not None:
            self._news_poller.stop()
        if self._ws_stream is not None and self._ws_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws_stream.stop(), self._ws_loop).result(5.0)
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
            except Exception:
                log.exception("error stopping live stream")
        self.bus.shutdown()


def _build_approval_gate(settings) -> ApprovalGate:
    """Use Telegram if both token + chat id are set; otherwise auto-approve."""
    tg = settings.telegram
    if tg.telegram_bot_token and tg.telegram_chat_id and tg.human_approval_required:
        try:
            from comms.telegram_bot import PythonTelegramBotTransport, TelegramApprovalGate
            transport = PythonTelegramBotTransport(tg.telegram_bot_token, int(tg.telegram_chat_id))
            log.info("Telegram approval gate enabled")
            return TelegramApprovalGate(transport)
        except Exception as exc:
            log.warning("Telegram gate unavailable (%s); falling back to NullApprovalGate", exc)
    return NullApprovalGate()


def _make_scorer() -> Scorer:
    """Try FinBERT; fall back to NullScorer if transformers/torch missing."""
    try:
        scorer = FinBertScorer()
        scorer._ensure_loaded()  # noqa: SLF001
        log.info("FinBERT loaded")
        return scorer
    except Exception as exc:
        log.warning("FinBERT unavailable (%s); using NullScorer", exc)
        return NullScorer()


def _enabled_agents(ctx: AppContext, scorer: Scorer) -> Iterable[Agent]:
    toggles = ctx.settings.agents

    if toggles.enable_research_india:
        yield ResearchIndia(
            feed=ctx.india_feed(),
            research_log=ctx.research_log,
            scorer=scorer,
        )

    if toggles.enable_research_crypto:
        yield ResearchCrypto(feed=ctx.crypto_feed(), research_log=ctx.research_log)

    if toggles.enable_trading_funding:
        yield TradingFunding(
            research_log=ctx.research_log,
            track_record=ctx.track_record,
            trade_router=ctx.trade_router,
        )

    if toggles.enable_trading_momentum:
        yield TradingMomentum(
            feed=ctx.india_feed(),
            research_log=ctx.research_log,
            track_record=ctx.track_record,
            trade_router=ctx.trade_router,
        )

    if toggles.enable_trading_sentiment:
        yield TradingSentiment(
            feed=ctx.india_feed(),
            research_log=ctx.research_log,
            track_record=ctx.track_record,
            trade_router=ctx.trade_router,
        )

    if toggles.enable_trading_pairs:
        yield TradingPairs(
            feed=ctx.india_feed(),
            research_log=ctx.research_log,
            track_record=ctx.track_record,
            trade_router=ctx.trade_router,
        )

    if toggles.enable_trading_trend:
        yield TradingTrend(
            feed=ctx.crypto_feed(),
            research_log=ctx.research_log,
            track_record=ctx.track_record,
            trade_router=ctx.trade_router,
        )

    if toggles.enable_trading_crypto_sent:
        yield TradingCryptoSent(research_log=ctx.research_log)


def _safe_run(agent: Agent) -> None:
    try:
        agent.run_once()
    except NotImplementedError as exc:
        log.info("[%s] not implemented yet: %s", agent.name, exc)
    except Exception:
        log.exception("[%s] tick failed", agent.name)


# ---------------------------------------------------------------- entrypoint

def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.runtime.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("alphagrid starting; paper_mode=%s", settings.runtime.paper_mode)

    ctx = AppContext()
    scorer = _make_scorer()

    # Tick + news speed — daemon threads, run independent of the scheduler.
    ctx.start_live_crypto_stream()
    ctx.start_news_poller(scorer)

    # Bar speed — APScheduler.
    scheduler = BlockingScheduler(timezone="UTC")
    for agent in _enabled_agents(ctx, scorer):
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
        ctx.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
