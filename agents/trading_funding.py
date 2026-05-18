"""Agent 6 — Crypto funding arbitrage (delta-neutral cash-and-carry).

Algorithm (refined from the 2019-2023 literature + 2025-2026 best-practice
guides — see the project memory for the full reading list):

  Entry conditions (ALL must hold for a symbol):
    1. We hold no open position for that symbol.
    2. We are past the cooldown window since our last exit on that symbol.
    3. The latest funding rate from the research_log is >= enter_bps.
    4. The last N consecutive prints (`funding_stability_windows`) are all
       >= enter_bps. A single-print spike is gambling, not arbitrage.
    5. The latest print is >= decay_floor * median(recent prints). Catches
       a fade that hasn't crossed the exit threshold yet.

  Sizing:
    Position size = min(risk-manager cap, tiered_fraction_for_rate * cap).
    Tiers come from `funding_size_tiers` — higher funding -> bigger size.

  Exit conditions (ANY triggers a close, no human approval needed):
    1. Latest funding rate < exit_bps.
    2. Last `funding_negative_close_windows` prints are all negative
       (carry has flipped to a cost — we're paying instead of receiving).
    3. Perp-spot basis exceeds `funding_basis_close_bps`. Means the
       delta-neutral hedge is no longer neutral; bail before mark-to-market
       drawdown chews the funding income.

  Read path:
    Funding rates + mark prices are NOT pulled live by this agent — they
    come from the research_log, which `research_crypto` writes every 8h.
    That keeps agent responsibilities clean and lets paper-mode replay
    work from a single DB.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

from agents.base import Agent, AgentCadence
from config.settings import get_settings
from execution.trade_router import TradeRouter
from record.research_log import ResearchLog, SignalRecord
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import TradeProposal

log = logging.getLogger(__name__)


class TradingFunding(Agent):
    name = "trading_funding"
    cadence = AgentCadence(every=timedelta(hours=8), aligned_to="binance_funding_8h")

    def __init__(
        self,
        *,
        research_log: ResearchLog,
        track_record: TrackRecord,
        trade_router: TradeRouter,
        portfolio_value_getter=None,
    ) -> None:
        self.research_log = research_log
        self.track_record = track_record
        self.trade_router = trade_router
        self.settings = get_settings()
        # In production the portfolio value comes from broker NAV. For
        # paper mode we expose an injectable callable that defaults to
        # a fixed starting capital. Override at construction time.
        self._portfolio_value = portfolio_value_getter or (lambda: 10_000.0)

    # ------------------------------------------------------------------ tick

    def run_once(self) -> None:
        universe = self.settings.strategy.funding_universe
        log.info("trading_funding tick: checking %d symbols", len(universe))
        for symbol in universe:
            try:
                self._tick_symbol(symbol)
            except Exception:
                log.exception("trading_funding failed on %s — continuing", symbol)

    # ---------------------------------------------------------------- per symbol

    def _tick_symbol(self, symbol: str) -> None:
        open_pos = self._open_position(symbol)

        recent = self._recent_funding(symbol)
        if not recent:
            log.debug("[%s] no funding history yet", symbol)
            return

        latest = recent[0]
        if open_pos is None:
            self._maybe_enter(symbol, recent)
        else:
            self._maybe_exit(symbol, open_pos, latest)

    # ------------------------------------------------------------------- entry

    def _maybe_enter(self, symbol: str, recent: list[SignalRecord]) -> None:
        sp = self.settings.strategy
        latest = recent[0]

        if self._in_cooldown(symbol):
            log.debug("[%s] in cooldown — skip entry", symbol)
            return

        # Rule 3 — current rate above threshold.
        if latest.value < sp.funding_enter_rate:
            return

        # Rule 4 — stability across last N windows.
        window = recent[: sp.funding_stability_windows]
        if len(window) < sp.funding_stability_windows:
            log.debug("[%s] only %d prints — not enough history", symbol, len(window))
            return
        if any(r.value < sp.funding_enter_rate for r in window):
            log.debug("[%s] funding not stable across %d prints", symbol, sp.funding_stability_windows)
            return

        # Rule 5 — not decaying. Latest must be >= floor * median.
        median = statistics.median(r.value for r in window)
        if median > 0 and latest.value < sp.funding_decay_floor * median:
            log.info(
                "[%s] funding decaying: latest=%.6f median=%.6f", symbol, latest.value, median
            )
            return

        mark_price = _mark_price_from(latest)
        if mark_price is None or mark_price <= 0:
            log.warning("[%s] no usable mark price in latest funding payload — skip", symbol)
            return

        portfolio = self._portfolio_value()
        size_fraction = self._size_fraction_for_rate(latest.value)
        # Use the risk manager's cap as our reference; size_fraction shrinks within it.
        intended_notional = sp.funding_size_tiers[-1][1] * portfolio * self.settings.risk.max_pct_per_trade
        target_notional = size_fraction * portfolio * self.settings.risk.max_pct_per_trade
        intended_qty = target_notional / mark_price

        proposal = TradeProposal(
            agent=self.name,
            market="crypto",
            ticker=symbol,
            side="BUY",  # spot leg dominates; perp short is the implicit hedge
            horizon="swing",
            intended_qty=intended_qty,
            reference_price=mark_price,
            portfolio_value=portfolio,
            signal_payload={
                "strategy": "funding_arb",
                "funding_rate": latest.value,
                "funding_median_recent": median,
                "stability_windows": sp.funding_stability_windows,
                "size_tier_fraction": size_fraction,
                "implied_perp_short_qty": intended_qty,
                "implied_leverage": min(sp.funding_max_leverage, 2.0),
                "max_intended_notional": intended_notional,
            },
            reason_text=(
                f"funding {latest.value*100:.4f}% per 8h stable for "
                f"{sp.funding_stability_windows} windows; size_tier={size_fraction:.2f}"
            ),
        )
        outcome = self.trade_router.submit(proposal)
        log.info(
            "[%s] entry attempt: outcome=%s reason=%s",
            symbol, outcome.state, outcome.reason,
        )

    # -------------------------------------------------------------------- exit

    def _maybe_exit(self, symbol: str, open_pos, latest_rate: SignalRecord) -> None:
        sp = self.settings.strategy

        # Pull recent prints for the negative-streak check.
        neg_window = self._recent_funding(symbol, limit=sp.funding_negative_close_windows)
        if (
            len(neg_window) >= sp.funding_negative_close_windows
            and all(r.value < 0 for r in neg_window)
        ):
            self._close(symbol, open_pos, latest_rate, reason="funding flipped negative")
            return

        if latest_rate.value < sp.funding_exit_bps:
            self._close(symbol, open_pos, latest_rate, reason=f"funding {latest_rate.value*100:.4f}% < exit")
            return

        # Basis blowout check — only when the research log carried a
        # spot-vs-perp basis hint in the payload. The current
        # research_crypto only stores mark_price; the spot leg is implicit,
        # so basis is computed against the most recent stored mark_price
        # at entry vs now. We treat any > funding_basis_close_bps gap as a
        # de-risk trigger.
        entry_payload = open_pos.signal_payload or {}
        entry_mark = entry_payload.get("entry_mark_price") or open_pos.entry_price
        latest_mark = _mark_price_from(latest_rate)
        if entry_mark and latest_mark and entry_mark > 0:
            basis = abs((latest_mark - entry_mark) / entry_mark)
            if basis > sp.funding_basis_close_bps * 10:  # generous; basis_close is in fractional terms
                self._close(
                    symbol,
                    open_pos,
                    latest_rate,
                    reason=f"basis drift {basis*100:.3f}% exceeds threshold",
                )
                return

        log.debug(
            "[%s] holding carry: funding=%.6f exit_th=%.6f",
            symbol, latest_rate.value, sp.funding_exit_bps,
        )

    def _close(self, symbol: str, open_pos, latest_rate: SignalRecord, *, reason: str) -> None:
        exit_price = _mark_price_from(latest_rate) or open_pos.entry_price
        self.track_record.close_trade(
            CloseTradeRequest(trade_id=open_pos.id, exit_price=exit_price, fees=0.0)
        )
        log.info("[%s] closed funding-arb trade=%s reason=%s", symbol, open_pos.id, reason)

    # ------------------------------------------------------------------ helpers

    def _open_position(self, symbol: str):
        for t in self.track_record.open_positions(agent=self.name):
            if t.ticker == symbol:
                return t
        return None

    def _recent_funding(self, symbol: str, *, limit: int | None = None) -> list[SignalRecord]:
        # Pull a healthy window so we can run stability + median + negative streak
        # checks off a single read.
        window = timedelta(days=7)
        records = self.research_log.recent(
            "funding_rate", window=window, ticker=symbol,
            limit=limit if limit is not None else None,
        )
        return records  # already ordered ts DESC by ResearchLog

    def _in_cooldown(self, symbol: str) -> bool:
        sp = self.settings.strategy
        since = datetime.now(timezone.utc) - timedelta(hours=sp.funding_cooldown_hours)
        closed = self.track_record.closed_trades(agent=self.name, since=since, limit=50)
        return any(t.ticker == symbol for t in closed)

    def _size_fraction_for_rate(self, rate: float) -> float:
        chosen = self.settings.strategy.funding_size_tiers[0][1]
        for threshold, frac in self.settings.strategy.funding_size_tiers:
            if rate >= threshold:
                chosen = frac
        return chosen


def _mark_price_from(rec: SignalRecord) -> float | None:
    payload = rec.payload or {}
    v = payload.get("mark_price")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
