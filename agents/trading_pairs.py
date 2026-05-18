"""Agent 5 — Indian pairs arbitrage (cointegration + Z-score mean reversion).

Algorithm (refined from QuantInsti EPAT 2015-2025 NSE backtests and the
2024 OU-application paper — see [[project-pairs-refinements]]):

  Weekly refit (per pair, see `pairs_refit_days`):
    1. Pearson correlation pre-screen — drop pairs with |r| < min_correlation.
    2. Engle-Granger two-step: OLS hedge ratio β, ADF p-value on residuals.
    3. Drop if p_value >= cointegration_pvalue.
    4. Estimate OU half-life on residuals. Drop if > half_life_max_days
       (mean reversion is too slow for our 15-day max holding).
    5. Cache β, μ, σ of the spread for the Z-score computation that follows.

  Each tick (every 30 min during the IST session):
    For each cached pair, fetch current closes, compute spread + rolling Z:
      Entry — open a new pair trade when |z| >= zscore_entry AND we don't
              already hold this pair. If z > 0, the spread is above mean
              (Y rich vs X), so SHORT Y and LONG X. If z < 0, opposite.
      Exit  — close the pair trade when:
                * |z| <= zscore_exit  (mean reversion completed),
                * |z| >= zscore_stop  (regime broken, take the loss),
                * holding > max_holding_days (time stop).

  Sizing:
    Equal-notional legs. Total notional capped by the risk manager's
    `max_pct_per_trade` cap. Each leg is half of the total.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from data.feeds_india import IndiaFeed
from execution.trade_router import TradeRouter
from models.pairs import correlation, engle_granger, ou_half_life, rolling_zscore
from record.research_log import ResearchLog, WriteSignal
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import TradeProposal

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class TradingPairs(Agent):
    name = "trading_pairs"
    cadence = AgentCadence(every=timedelta(minutes=30))

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
        # cache: pair_key -> {beta, intercept, fit_at, half_life}
        self._fits: dict[str, dict] = {}

    # ------------------------------------------------------------------- tick

    def run_once(self) -> None:
        if not self._inside_trading_window():
            return
        for pair in self.settings.strategy.pairs_universe:
            try:
                self._tick_pair(pair)
            except Exception:
                log.exception("trading_pairs failed on %s — continuing", pair)

    # --------------------------------------------------------------- per pair

    def _tick_pair(self, pair: tuple[str, str]) -> None:
        sp = self.settings.strategy
        key = self._key(pair)

        fit = self._fits.get(key)
        if fit is None or self._needs_refit(fit):
            fit = self._refit(pair)
            if fit is None:
                return
            self._fits[key] = fit

        closes_x, closes_y = self._fetch_aligned_closes(pair)
        if closes_x is None or closes_y is None:
            return

        beta = fit["beta"]
        intercept = fit["intercept"]
        spread = [y - (intercept + beta * x) for x, y in zip(closes_x, closes_y, strict=False)]
        zs = rolling_zscore(spread, sp.pairs_zscore_window)
        z_now = zs[-1]
        if z_now is None:
            return

        held = self._open_pair(key)
        if held is not None:
            self._maybe_exit(pair, held, z_now)
            return

        self._maybe_enter(pair, z_now, closes_x[-1], closes_y[-1], fit)

    # ------------------------------------------------------------------ entry

    def _maybe_enter(
        self,
        pair: tuple[str, str],
        z_now: float,
        x_price: float,
        y_price: float,
        fit: dict,
    ) -> None:
        sp = self.settings.strategy
        if abs(z_now) < sp.pairs_zscore_entry:
            return
        if abs(z_now) >= sp.pairs_zscore_stop:
            # already past the stop band — don't enter, regime broken
            log.info("[%s/%s] z=%.3f past stop band, skip entry", pair[0], pair[1], z_now)
            return

        portfolio = self._portfolio_value()
        total_notional = self.settings.risk.max_pct_per_trade * portfolio
        leg_notional = 0.5 * total_notional

        # z > 0 -> y is rich -> short y, long x. z < 0 -> opposite.
        if z_now > 0:
            short_ticker, short_price = pair[1], y_price
            long_ticker, long_price = pair[0], x_price
        else:
            short_ticker, short_price = pair[0], x_price
            long_ticker, long_price = pair[1], y_price

        long_qty = leg_notional / long_price if long_price > 0 else 0.0
        short_qty = leg_notional / short_price if short_price > 0 else 0.0

        pair_key = self._key(pair)
        common_payload = {
            "strategy": "pairs_arb",
            "pair_key": pair_key,
            "pair": list(pair),
            "z_at_entry": z_now,
            "hedge_ratio": fit["beta"],
            "intercept": fit["intercept"],
            "half_life": fit["half_life"],
            "entry_ts": self._now().isoformat(),
            "long_leg": long_ticker,
            "short_leg": short_ticker,
        }

        long_outcome = self.trade_router.submit(TradeProposal(
            agent=self.name,
            market="india",
            ticker=long_ticker,
            side="BUY",
            horizon="swing",
            intended_qty=long_qty,
            reference_price=long_price,
            portfolio_value=portfolio,
            signal_payload={**common_payload, "leg": "long"},
            reason_text=f"pairs Z={z_now:.2f}, LONG {long_ticker} / SHORT {short_ticker}",
        ))
        short_outcome = self.trade_router.submit(TradeProposal(
            agent=self.name,
            market="india",
            ticker=short_ticker,
            side="SHORT",
            horizon="swing",
            intended_qty=short_qty,
            reference_price=short_price,
            portfolio_value=portfolio,
            signal_payload={**common_payload, "leg": "short"},
            reason_text=f"pairs Z={z_now:.2f}, SHORT {short_ticker} / LONG {long_ticker}",
        ))
        log.info(
            "[%s] pairs entry: long=%s short=%s outcomes=(%s, %s)",
            pair_key, long_ticker, short_ticker, long_outcome.state, short_outcome.state,
        )

    # ------------------------------------------------------------------- exit

    def _maybe_exit(self, pair: tuple[str, str], held: list, z_now: float) -> None:
        sp = self.settings.strategy
        if not held:
            return
        # All legs share the same entry_ts in their payload.
        payload = held[0].signal_payload or {}
        entry_ts_str = payload.get("entry_ts")
        if entry_ts_str:
            try:
                entry_ts = datetime.fromisoformat(entry_ts_str)
            except ValueError:
                entry_ts = held[0].entry_ts
        else:
            entry_ts = held[0].entry_ts
        if entry_ts is not None and entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)

        reason: str | None = None
        if abs(z_now) <= sp.pairs_zscore_exit:
            reason = f"converged z={z_now:.3f}"
        elif abs(z_now) >= sp.pairs_zscore_stop:
            reason = f"regime break z={z_now:.3f}"
        elif entry_ts is not None:
            held_days = (self._now() - entry_ts).total_seconds() / 86400.0
            if held_days >= sp.pairs_max_holding_days:
                reason = f"time stop held {held_days:.1f}d"

        if reason is None:
            return

        for leg in held:
            self.track_record.close_trade(
                CloseTradeRequest(trade_id=leg.id, exit_price=leg.entry_price)
            )
        log.info("[%s/%s] pairs closed: %s", pair[0], pair[1], reason)

    # ----------------------------------------------------------------- refits

    def _refit(self, pair: tuple[str, str]) -> dict | None:
        sp = self.settings.strategy
        closes_x, closes_y = self._fetch_aligned_closes(
            pair, days=sp.pairs_lookback_bars
        )
        if (
            closes_x is None or closes_y is None
            or len(closes_x) < sp.pairs_lookback_bars * 0.8
        ):
            log.debug("[%s] not enough history to refit", self._key(pair))
            return None

        # Pre-screen by correlation.
        corr = correlation(closes_x, closes_y)
        if abs(corr) < sp.pairs_min_correlation:
            log.info("[%s] correlation %.2f below %.2f", self._key(pair), corr, sp.pairs_min_correlation)
            return None

        fit = engle_granger(closes_x, closes_y, p_threshold=sp.pairs_cointegration_pvalue)
        if not fit.cointegrated:
            log.info("[%s] not cointegrated (p=%.3f)", self._key(pair), fit.p_value)
            return None

        hl = ou_half_life(fit.residuals)
        if hl is None or hl > sp.pairs_half_life_max_days:
            log.info(
                "[%s] OU half-life unsuitable (%s)", self._key(pair),
                "no mean reversion" if hl is None else f"{hl:.1f}d",
            )
            return None

        log.info(
            "[%s] refit ok: β=%.3f p=%.3f half_life=%.1fd corr=%.2f",
            self._key(pair), fit.hedge_ratio, fit.p_value, hl, corr,
        )
        # Record the refit on the research log so an auditor can see when
        # we last accepted this pair as tradable.
        self.research_log.write(WriteSignal(
            agent=self.name,
            market="india",
            ticker=self._key(pair),
            signal_type="pairs_fit",
            value=fit.p_value,
            payload={
                "beta": fit.hedge_ratio,
                "intercept": fit.intercept,
                "half_life_days": hl,
                "correlation": corr,
            },
            ts=self._now(),
        ))
        return {
            "beta": fit.hedge_ratio,
            "intercept": fit.intercept,
            "p_value": fit.p_value,
            "half_life": hl,
            "fit_at": self._now(),
        }

    def _needs_refit(self, fit: dict) -> bool:
        sp = self.settings.strategy
        fit_at = fit.get("fit_at")
        if fit_at is None:
            return True
        if fit_at.tzinfo is None:
            fit_at = fit_at.replace(tzinfo=timezone.utc)
        age = (self._now() - fit_at).total_seconds() / 86400.0
        return age >= sp.pairs_refit_days

    # ----------------------------------------------------------------- helpers

    def _fetch_aligned_closes(
        self, pair: tuple[str, str], *, days: int | None = None
    ) -> tuple[list[float] | None, list[float] | None]:
        sp = self.settings.strategy
        n = days or sp.pairs_lookback_bars
        try:
            x_bars = self.feed.fetch_ohlc(pair[0], days=n)
            y_bars = self.feed.fetch_ohlc(pair[1], days=n)
        except Exception:
            log.exception("[%s/%s] OHLC fetch failed", pair[0], pair[1])
            return None, None
        if not x_bars or not y_bars:
            return None, None
        # Align by index from the end — both lists already sorted ts ASC.
        n_common = min(len(x_bars), len(y_bars))
        x_closes = [b.bar.close for b in x_bars[-n_common:]]
        y_closes = [b.bar.close for b in y_bars[-n_common:]]
        return x_closes, y_closes

    def _open_pair(self, key: str) -> list:
        legs = []
        for t in self.track_record.open_positions(agent=self.name):
            payload = t.signal_payload or {}
            if payload.get("pair_key") == key:
                legs.append(t)
        return legs

    def _inside_trading_window(self) -> bool:
        now_ist = self._now().astimezone(IST)
        cur = now_ist.hour * 60 + now_ist.minute
        return 9 * 60 + 30 <= cur <= 15 * 60 + 25

    @staticmethod
    def _key(pair: tuple[str, str]) -> str:
        return f"{pair[0]}/{pair[1]}"


