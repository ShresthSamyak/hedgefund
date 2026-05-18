"""Agent 7 — Crypto trend following with vol targeting.

Algorithm (refined from the 2025 Bitcoin trend benchmark and on-chain
literature):

  For each symbol in trend_universe (BTC/USDT, ETH/USDT, SOL/USDT), pull
  4-hour OHLC bars from the feed. Compute three speed pairs:
      (8, 32)   — fast
      (16, 64)  — medium
      (32, 128) — slow

  A speed pair "votes bullish" when fast_ewma_now > slow_ewma_now,
  "bearish" when fast < slow. Position direction = majority vote with at
  least `trend_min_speeds_agreeing` (=2) speeds agreeing. Ties are flat.

  Sizing: inverse realized volatility, targeting `trend_target_portfolio_vol`
  (=10% annualized) per symbol, capped at `trend_max_leverage` (3x). Then
  multiplied by the latest `crypto_size_modifier` from the regime gate
  (∈ [-1, +1], scales notional by ±20% by default).

  Exit when the majority vote flips OR drops below the agreement threshold.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import timedelta

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_crypto import CryptoFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar, ewma
from record.research_log import ResearchLog
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import TradeProposal

log = logging.getLogger(__name__)

# 4-hour bars -> 6 per day -> 365*6 ≈ 2190 bars per year.
BARS_PER_YEAR_4H = 365 * 6


class TradingTrend(Agent):
    name = "trading_trend"
    cadence = AgentCadence(every=timedelta(hours=1))

    def __init__(
        self,
        *,
        feed: CryptoFeed,
        research_log: ResearchLog,
        track_record: TrackRecord,
        trade_router: TradeRouter,
        portfolio_value_getter=None,
    ) -> None:
        self.feed = feed
        self.research_log = research_log
        self.track_record = track_record
        self.trade_router = trade_router
        self.settings = get_settings()
        self._portfolio_value = portfolio_value_getter or (lambda: 10_000.0)

    # ---------------------------------------------------------------- run_once

    def run_once(self) -> None:
        for symbol in self.settings.strategy.trend_universe:
            try:
                self._tick_symbol(symbol)
            except Exception:
                log.exception("trading_trend failed on %s — continuing", symbol)

    # ---------------------------------------------------------------- per symbol

    def _tick_symbol(self, symbol: str) -> None:
        sp = self.settings.strategy
        bars = self._fetch_bars(symbol)
        if bars is None:
            return

        closes = [b.close for b in bars]
        votes = self._compute_votes(closes)
        bullish_votes = sum(1 for v in votes if v == "bullish")
        bearish_votes = sum(1 for v in votes if v == "bearish")
        majority = (
            "bullish" if bullish_votes >= sp.trend_min_speeds_agreeing
            else "bearish" if bearish_votes >= sp.trend_min_speeds_agreeing
            else None
        )

        open_pos = self._open_position(symbol)
        if open_pos is not None:
            self._maybe_exit(symbol, open_pos, majority, closes[-1])
            return

        if majority is None:
            return

        # Sizing.
        current_price = closes[-1]
        realized_vol = _annualized_vol(closes, sp.trend_vol_lookback)
        if realized_vol is None or realized_vol <= 0:
            log.debug("[%s] realized vol unavailable — skip", symbol)
            return
        portfolio = self._portfolio_value()
        # Vol-target sizing: notional = (target_vol / realized_vol) * portfolio
        target_leverage = sp.trend_target_portfolio_vol / realized_vol
        leverage = min(target_leverage, sp.trend_max_leverage)
        size_modifier = self._size_modifier()
        notional = leverage * portfolio * (1.0 + sp.crypto_sent_size_modifier * size_modifier)
        # Final cap from the risk manager (still applied downstream).
        intended_qty = notional / current_price

        side = "BUY" if majority == "bullish" else "SHORT"
        proposal = TradeProposal(
            agent=self.name,
            market="crypto",
            ticker=symbol,
            side=side,
            horizon="swing",
            intended_qty=intended_qty,
            reference_price=current_price,
            portfolio_value=portfolio,
            signal_payload={
                "strategy": "trend_multispeed",
                "votes": votes,
                "majority": majority,
                "realized_vol": realized_vol,
                "leverage_applied": leverage,
                "size_modifier": size_modifier,
            },
            reason_text=(
                f"trend votes {votes} -> {majority}; "
                f"vol={realized_vol*100:.1f}% lev={leverage:.2f}x mod={size_modifier:+.2f}"
            ),
        )
        outcome = self.trade_router.submit(proposal)
        log.info("[%s] trend entry attempt: outcome=%s", symbol, outcome.state)

    def _maybe_exit(self, symbol: str, open_pos, majority: str | None, current_price: float) -> None:
        held_side = "bullish" if open_pos.side.upper() in {"BUY", "LONG"} else "bearish"
        if majority is None or majority != held_side:
            self.track_record.close_trade(
                CloseTradeRequest(trade_id=open_pos.id, exit_price=current_price)
            )
            log.info(
                "[%s] trend closed: held=%s majority=%s",
                symbol, held_side, majority,
            )

    # ---------------------------------------------------------------- helpers

    def _compute_votes(self, closes: list[float]) -> list[str | None]:
        sp = self.settings.strategy
        votes: list[str | None] = []
        for fast_p, slow_p in sp.trend_speeds:
            fast = ewma(closes, fast_p)
            slow = ewma(closes, slow_p)
            f_now = fast[-1]
            s_now = slow[-1]
            if f_now is None or s_now is None:
                votes.append(None)
            elif f_now > s_now:
                votes.append("bullish")
            elif f_now < s_now:
                votes.append("bearish")
            else:
                votes.append(None)
        return votes

    def _fetch_bars(self, symbol: str) -> list[OHLCBar] | None:
        sp = self.settings.strategy
        try:
            dated = self.feed.fetch_ohlc(symbol, timeframe="4h", limit=sp.trend_min_history_bars + 10)
        except Exception:
            log.exception("[%s] trend OHLC fetch failed", symbol)
            return None
        if len(dated) < sp.trend_min_history_bars:
            log.debug("[%s] only %d bars — need %d", symbol, len(dated), sp.trend_min_history_bars)
            return None
        return [d.bar for d in dated]

    def _open_position(self, symbol: str):
        for t in self.track_record.open_positions(agent=self.name):
            if t.ticker == symbol:
                return t
        return None

    def _size_modifier(self) -> float:
        """Read latest crypto_size_modifier from research_log. 0.0 if missing."""
        rec = self.research_log.latest("PORTFOLIO", "crypto_size_modifier")
        if rec is None:
            return 0.0
        return max(-1.0, min(1.0, rec.value))


def _annualized_vol(closes: list[float], lookback: int) -> float | None:
    """Annualized stdev of log returns over the last `lookback` bars.
    Assumes 4-hour bars (BARS_PER_YEAR_4H = 2190).
    """
    if len(closes) < lookback + 1:
        return None
    rets: list[float] = []
    for i in range(len(closes) - lookback, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    return sd * math.sqrt(BARS_PER_YEAR_4H)
