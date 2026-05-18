"""Agent 3 — Indian momentum (EWMA crossover, ATR sized, multi-filter).

Algorithm (refined from the 2025-2026 Indian-market literature — see
[[project-momentum-refinements]] memory):

  Entry (every filter must pass — raw cross wins ~45%, filtered 60-68%):
    1. We are inside the Indian intraday session and past the open-bell
       skip window (default 15 min).
    2. We hold no open position for that ticker.
    3. EWMA(8) crossed above EWMA(32) at the most recent bar.
    4. Price > EWMA(200) — higher-timeframe trend gate.
    5. ADX(14) > threshold (20) — skip choppy markets.
    6. Cross-bar volume >= volume_ratio_min x rolling-median volume.
    7. (optional) Latest research_india sentiment_score >= 0.

  Sizing & risk plan stored on the trade:
    * stop  = entry - atr_stop_mult * ATR(14)
    * target = entry + atr_target_mult * ATR(14)  (1:2 R:R by default)
    * qty   = (max_pct_per_trade * portfolio) / (entry - stop)
      then clipped by the risk manager's own cap.

  Exit (no human approval needed):
    1. Price <= stop  -> close
    2. Price >= target -> close
    3. EWMA(8) crossed back below EWMA(32) -> close (trend reversed)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_india import IndiaFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar, adx, atr, detect_cross, ewma
from record.research_log import ResearchLog
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import TradeProposal

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class TradingMomentum(Agent):
    name = "trading_momentum"
    cadence = AgentCadence(every=timedelta(minutes=5))

    def __init__(
        self,
        *,
        feed: IndiaFeed,
        research_log: ResearchLog,
        track_record: TrackRecord,
        trade_router: TradeRouter,
        portfolio_value_getter=None,
        now_fn=None,
    ) -> None:
        self.feed = feed
        self.research_log = research_log
        self.track_record = track_record
        self.trade_router = trade_router
        self.settings = get_settings()
        self._portfolio_value = portfolio_value_getter or (lambda: 10_000.0)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- run_once

    def run_once(self) -> None:
        if not self._inside_trading_window():
            log.debug("trading_momentum outside session window — skip")
            return
        universe = self.settings.strategy.momentum_universe
        for ticker in universe:
            try:
                self._tick_symbol(ticker)
            except Exception:
                log.exception("trading_momentum failed on %s — continuing", ticker)

    # --------------------------------------------------------------- per ticker

    def _tick_symbol(self, ticker: str) -> None:
        bars = self._fetch_bars(ticker)
        if bars is None:
            return

        sp = self.settings.strategy
        closes = [b.close for b in bars]
        fast = ewma(closes, sp.momentum_fast_ewma)
        slow = ewma(closes, sp.momentum_slow_ewma)
        trend = ewma(closes, sp.momentum_trend_ema)
        atrs = atr(bars, sp.momentum_atr_period)
        adxs = adx(bars, sp.momentum_adx_period)

        cross = detect_cross(fast, slow)
        current_price = closes[-1]
        current_atr = atrs[-1]
        current_adx = adxs[-1]
        current_trend = trend[-1]

        open_pos = self._open_position(ticker)
        if open_pos is not None:
            self._maybe_exit(ticker, open_pos, current_price, cross)
            return

        # ENTRY GATES ----------------------------------------------------------
        if cross != "bullish":
            log.debug("[%s] no bullish cross", ticker)
            return
        if current_trend is None or current_price <= current_trend:
            log.debug("[%s] price below 200-EMA — skip", ticker)
            return
        if current_adx is None or current_adx < sp.momentum_adx_threshold:
            log.debug("[%s] ADX %.2f below threshold %.2f — chop, skip", ticker, current_adx or 0.0, sp.momentum_adx_threshold)
            return
        if not self._volume_confirmed(bars):
            log.debug("[%s] volume not confirmed — skip", ticker)
            return
        if sp.momentum_require_nonnegative_sentiment and not self._sentiment_ok(ticker):
            log.debug("[%s] sentiment negative — skip", ticker)
            return
        if current_atr is None or current_atr <= 0:
            log.debug("[%s] ATR not ready — skip", ticker)
            return

        # SIZING ---------------------------------------------------------------
        stop_distance = sp.momentum_atr_stop_mult * current_atr
        stop_price = current_price - stop_distance
        target_price = current_price + sp.momentum_atr_target_mult * current_atr
        portfolio = self._portfolio_value()
        risk_budget = self.settings.risk.max_pct_per_trade * portfolio
        intended_qty = risk_budget / stop_distance

        proposal = TradeProposal(
            agent=self.name,
            market="india",
            ticker=ticker,
            side="BUY",
            horizon="swing",
            intended_qty=intended_qty,
            reference_price=current_price,
            portfolio_value=portfolio,
            signal_payload={
                "strategy": "momentum_ewma_cross",
                "fast_ewma": fast[-1],
                "slow_ewma": slow[-1],
                "trend_ewma": current_trend,
                "atr": current_atr,
                "adx": current_adx,
                "stop_price": stop_price,
                "target_price": target_price,
                "risk_reward": sp.momentum_atr_target_mult / sp.momentum_atr_stop_mult,
            },
            reason_text=(
                f"EWMA{sp.momentum_fast_ewma}>{sp.momentum_slow_ewma} cross, "
                f"price>{sp.momentum_trend_ema}-EMA, ADX={current_adx:.1f}, "
                f"stop={stop_price:.2f} target={target_price:.2f}"
            ),
        )
        outcome = self.trade_router.submit(proposal)
        log.info(
            "[%s] momentum entry attempt: outcome=%s reason=%s",
            ticker, outcome.state, outcome.reason,
        )

    def _maybe_exit(self, ticker: str, open_pos, current_price: float, cross: str | None) -> None:
        payload = open_pos.signal_payload or {}
        stop_price = payload.get("stop_price")
        target_price = payload.get("target_price")

        if stop_price is not None and current_price <= stop_price:
            self._close(ticker, open_pos, current_price, reason=f"stop hit @ {stop_price:.2f}")
            return
        if target_price is not None and current_price >= target_price:
            self._close(ticker, open_pos, current_price, reason=f"target hit @ {target_price:.2f}")
            return
        if cross == "bearish":
            self._close(ticker, open_pos, current_price, reason="reverse cross")
            return

    def _close(self, ticker: str, open_pos, exit_price: float, *, reason: str) -> None:
        self.track_record.close_trade(
            CloseTradeRequest(trade_id=open_pos.id, exit_price=exit_price, fees=0.0)
        )
        log.info("[%s] momentum closed trade=%s reason=%s", ticker, open_pos.id, reason)

    # ------------------------------------------------------------------ helpers

    def _fetch_bars(self, ticker: str) -> list[OHLCBar] | None:
        sp = self.settings.strategy
        try:
            dated = self.feed.fetch_ohlc(ticker, days=sp.momentum_min_history_bars + 10)
        except Exception:
            log.exception("[%s] OHLC fetch failed", ticker)
            return None
        if len(dated) < sp.momentum_min_history_bars:
            log.debug("[%s] only %d bars — need %d", ticker, len(dated), sp.momentum_min_history_bars)
            return None
        return [d.bar for d in dated]

    def _volume_confirmed(self, bars: list[OHLCBar]) -> bool:
        sp = self.settings.strategy
        lookback = sp.momentum_volume_lookback
        if len(bars) < lookback + 1:
            return True  # not enough volume history -> don't block
        recent_vols = sorted(b.volume for b in bars[-lookback - 1 : -1])
        if not recent_vols:
            return True
        median = recent_vols[len(recent_vols) // 2]
        if median <= 0:
            return True
        return bars[-1].volume >= sp.momentum_volume_ratio_min * median

    def _sentiment_ok(self, ticker: str) -> bool:
        rec = self.research_log.latest(ticker, "sentiment_score")
        if rec is None:
            return True  # no sentiment yet -> don't block
        return rec.value >= 0.0

    def _open_position(self, ticker: str):
        for t in self.track_record.open_positions(agent=self.name):
            if t.ticker == ticker:
                return t
        return None

    def _inside_trading_window(self) -> bool:
        sp = self.settings.strategy
        now_ist = self._now().astimezone(IST)
        open_t = (9, 15)
        close_t = (15, 25)
        # Account for skip-first-N-minutes of the open.
        skip_until_h = open_t[0]
        skip_until_m = open_t[1] + sp.momentum_skip_first_minutes
        if skip_until_m >= 60:
            skip_until_h += skip_until_m // 60
            skip_until_m = skip_until_m % 60
        cur = now_ist.hour * 60 + now_ist.minute
        start = skip_until_h * 60 + skip_until_m
        end = close_t[0] * 60 + close_t[1]
        return start <= cur <= end
