"""Agent 4 — Indian sentiment trading.

Reads `sentiment_score` records that `research_india` writes every 15 min,
applies a decay-weighted stability check, and enters small long positions
when retail-driven overreaction is sustained — exiting before the
mean-reversion phase eats the gain.

Refinements from the 2025-2026 literature (see [[project-sentiment-refinements]]):
  Entry — ALL must pass:
    1. Inside IST session, past the open-bell skip window.
    2. No open position for this ticker.
    3. Past the cooldown window since our last exit on this ticker.
    4. Latest sentiment_score >= entry_threshold (signal is hot NOW).
    5. Latest record had at least `sentiment_min_headlines` headlines —
       single-headline signals are too noisy.
    6. Time-decay-weighted average over the last N records >= threshold.
       Catches stale signals: a hot latest print backed by neutral history
       doesn't pass.
    7. Cross-bar volume >= rolling-median volume.

  Sizing:
    Smaller than momentum (architecture spec: "small position"). We size
    by half the per-trade cap and a tighter 1.5x ATR stop, 3.0x ATR target.

  Exit — ANY triggers a close (no human approval needed):
    1. Sentiment record <= panic_threshold (-0.30) — bad news asymmetry.
    2. Latest sentiment <= exit_threshold (0.50).
    3. Position held longer than max_holding_days.
    4. Price hit stop or target.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_india import IndiaFeed
from execution.trade_router import TradeRouter
from models.indicators import OHLCBar, atr
from record.research_log import ResearchLog, SignalRecord
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import TradeProposal

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class TradingSentiment(Agent):
    name = "trading_sentiment"
    cadence = AgentCadence(every=timedelta(minutes=15))

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

    # ------------------------------------------------------------------ tick

    def run_once(self) -> None:
        if not self._inside_trading_window():
            log.debug("trading_sentiment outside session window — skip")
            return
        universe = self.settings.strategy.sentiment_universe
        for ticker in universe:
            try:
                self._tick_symbol(ticker)
            except Exception:
                log.exception("trading_sentiment failed on %s — continuing", ticker)

    # ---------------------------------------------------------------- per ticker

    def _tick_symbol(self, ticker: str) -> None:
        open_pos = self._open_position(ticker)
        recent = self._recent_sentiment(ticker)

        if open_pos is not None:
            self._maybe_exit(ticker, open_pos, recent)
            return

        self._maybe_enter(ticker, recent)

    # ------------------------------------------------------------------- entry

    def _maybe_enter(self, ticker: str, recent: list[SignalRecord]) -> None:
        sp = self.settings.strategy

        if self._in_cooldown(ticker):
            log.debug("[%s] sentiment cooldown — skip", ticker)
            return

        window = recent[: sp.sentiment_consecutive_windows]
        if len(window) < sp.sentiment_consecutive_windows:
            return

        latest = window[0]

        # Rule 4 — latest record is hot.
        if latest.value < sp.sentiment_entry_threshold:
            return

        # Rule 5 — latest record backed by enough headlines.
        if _headline_count(latest) < sp.sentiment_min_headlines:
            log.debug("[%s] latest record has thin headlines — skip", ticker)
            return

        # Rule 6 — decay-weighted recent average also above threshold.
        # If the older records were neutral, this drags weighted < threshold.
        weighted = _decay_weighted(window, sp.sentiment_decay_halflife_hours, self._now())
        if weighted < sp.sentiment_entry_threshold:
            log.debug("[%s] decay-weighted %.3f below threshold — stale signal", ticker, weighted)
            return

        # ATR for sizing + bars for volume confirmation.
        bars = self._fetch_bars(ticker)
        if bars is None:
            return
        if not self._volume_confirmed(bars):
            log.debug("[%s] sentiment: volume not confirmed — skip", ticker)
            return

        atrs = atr(bars, sp.sentiment_atr_period)
        current_atr = atrs[-1]
        current_price = bars[-1].close
        if current_atr is None or current_atr <= 0 or current_price <= 0:
            return

        stop_distance = sp.sentiment_atr_stop_mult * current_atr
        stop_price = current_price - stop_distance
        target_price = current_price + sp.sentiment_atr_target_mult * current_atr

        portfolio = self._portfolio_value()
        # Half the per-trade cap — architecture spec calls for "small position".
        risk_budget = 0.5 * self.settings.risk.max_pct_per_trade * portfolio
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
                "strategy": "sentiment_breakout",
                "decay_weighted_sentiment": weighted,
                "latest_sentiment": window[0].value,
                "consecutive_windows": sp.sentiment_consecutive_windows,
                "headline_min": sp.sentiment_min_headlines,
                "stop_price": stop_price,
                "target_price": target_price,
                "atr": current_atr,
                "entry_ts": self._now().isoformat(),
                "max_holding_days": sp.sentiment_max_holding_days,
            },
            reason_text=(
                f"sentiment {weighted:.3f} sustained for "
                f"{sp.sentiment_consecutive_windows} windows "
                f"({sp.sentiment_min_headlines}+ headlines each); "
                f"stop={stop_price:.2f} target={target_price:.2f}"
            ),
        )
        outcome = self.trade_router.submit(proposal)
        log.info(
            "[%s] sentiment entry attempt: outcome=%s reason=%s",
            ticker, outcome.state, outcome.reason,
        )

    # -------------------------------------------------------------------- exit

    def _maybe_exit(self, ticker: str, open_pos, recent: list[SignalRecord]) -> None:
        sp = self.settings.strategy
        payload = open_pos.signal_payload or {}

        # Rule 1 — panic exit on strong negative news.
        latest = recent[0] if recent else None
        if latest is not None and latest.value <= sp.sentiment_panic_threshold:
            self._close(ticker, open_pos, open_pos.entry_price, reason=f"panic: sentiment {latest.value:.2f}")
            return

        # Rule 2 — sentiment faded.
        if latest is not None and latest.value < sp.sentiment_exit_threshold:
            self._close(ticker, open_pos, open_pos.entry_price, reason=f"sentiment fade {latest.value:.2f}")
            return

        # Rule 3 — max holding window.
        entry_ts_str = payload.get("entry_ts")
        entry_ts: datetime | None
        if entry_ts_str:
            try:
                entry_ts = datetime.fromisoformat(entry_ts_str)
            except ValueError:
                entry_ts = open_pos.entry_ts
        else:
            entry_ts = open_pos.entry_ts
        if entry_ts is not None:
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.replace(tzinfo=timezone.utc)
            held = (self._now() - entry_ts).total_seconds() / 86400.0
            if held >= sp.sentiment_max_holding_days:
                self._close(ticker, open_pos, open_pos.entry_price, reason=f"held {held:.1f}d >= max")
                return

        # Rule 4 — price stop / target.
        try:
            bars = self.feed.fetch_ohlc(ticker, days=2)
        except Exception:
            log.exception("[%s] sentiment exit: OHLC fetch failed", ticker)
            bars = []
        if bars:
            price = bars[-1].bar.close
            stop = payload.get("stop_price")
            target = payload.get("target_price")
            if stop is not None and price <= stop:
                self._close(ticker, open_pos, price, reason=f"stop hit @ {stop:.2f}")
                return
            if target is not None and price >= target:
                self._close(ticker, open_pos, price, reason=f"target hit @ {target:.2f}")
                return

    def _close(self, ticker: str, open_pos, exit_price: float, *, reason: str) -> None:
        self.track_record.close_trade(
            CloseTradeRequest(trade_id=open_pos.id, exit_price=exit_price, fees=0.0)
        )
        log.info("[%s] sentiment closed trade=%s reason=%s", ticker, open_pos.id, reason)

    # ------------------------------------------------------------------ helpers

    def _recent_sentiment(self, ticker: str) -> list[SignalRecord]:
        return self.research_log.recent(
            "sentiment_score",
            window=timedelta(days=3),
            ticker=ticker,
            agent="research_india",
        )

    def _fetch_bars(self, ticker: str) -> list[OHLCBar] | None:
        sp = self.settings.strategy
        try:
            dated = self.feed.fetch_ohlc(ticker, days=sp.sentiment_min_history_bars + 5)
        except Exception:
            log.exception("[%s] sentiment OHLC fetch failed", ticker)
            return None
        if len(dated) < sp.sentiment_min_history_bars:
            return None
        return [d.bar for d in dated]

    def _volume_confirmed(self, bars: list[OHLCBar]) -> bool:
        sp = self.settings.strategy
        lookback = sp.sentiment_volume_lookback
        if len(bars) < lookback + 1:
            return True
        recent_vols = sorted(b.volume for b in bars[-lookback - 1 : -1])
        median = recent_vols[len(recent_vols) // 2]
        if median <= 0:
            return True
        return bars[-1].volume >= sp.sentiment_volume_ratio_min * median

    def _open_position(self, ticker: str):
        for t in self.track_record.open_positions(agent=self.name):
            if t.ticker == ticker:
                return t
        return None

    def _in_cooldown(self, ticker: str) -> bool:
        sp = self.settings.strategy
        since = self._now() - timedelta(hours=sp.sentiment_cooldown_hours)
        closed = self.track_record.closed_trades(agent=self.name, since=since, limit=50)
        return any(t.ticker == ticker for t in closed)

    def _inside_trading_window(self) -> bool:
        now_ist = self._now().astimezone(IST)
        cur = now_ist.hour * 60 + now_ist.minute
        # Use the same skip-15-min window as momentum.
        start = 9 * 60 + 30
        end = 15 * 60 + 25
        return start <= cur <= end


# ---------------------------------------------------------------- math helpers


def _headline_count(rec: SignalRecord) -> int:
    payload = rec.payload or {}
    return int(payload.get("headline_count", 0))


def _decay_weighted(records, halflife_hours: float, now: datetime) -> float:
    """Exponential decay: weight = 0.5 ^ (age_hours / halflife).

    SQLite drops tzinfo on read, so we normalize any naive timestamp to UTC
    before subtracting. Accepts anything with `.ts` and `.value` attributes.
    """
    if not records:
        return 0.0
    total_w = 0.0
    total_wv = 0.0
    for r in records:
        ts = r.ts if r.ts.tzinfo is not None else r.ts.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
        w = math.pow(0.5, age_hours / halflife_hours) if halflife_hours > 0 else 1.0
        total_w += w
        total_wv += w * r.value
    return total_wv / total_w if total_w > 0 else 0.0
