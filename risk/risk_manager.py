"""The trust boundary every trade crosses.

Six rules, locked from the architecture doc:
  1. Half-Kelly sizing from the agent's recent win rate / win-loss ratio.
  2. Hard cap of 2% of portfolio per trade.
  3. Kill switch on 10% rolling-30d drawdown.
  4. Correlation block — no duplicate longs; cap on correlated long exposure.
  5. Market-hours gate — Indian intraday closes 15:25 IST; crypto is 24/7.
  6. Regime override — crypto trend agents blocked when regime == "risk_off".

Single entry point: RiskManager.review(proposal) -> Decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from config.settings import get_settings
from record.track_record import TrackRecord

Side = Literal["BUY", "SELL", "LONG", "SHORT"]
Market = Literal["india", "crypto"]
Horizon = Literal["intraday", "swing"]
Regime = Literal["risk_on", "risk_off", "neutral"]


@dataclass(frozen=True)
class TradeProposal:
    agent: str
    market: Market
    ticker: str
    side: Side
    horizon: Horizon
    intended_qty: float
    reference_price: float
    portfolio_value: float
    signal_payload: dict
    reason_text: str
    # Optional: pre-computed correlations against currently open longs.
    correlation_with_open_longs: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    approved: bool
    sized_qty: float
    reason: str
    rule_trail: list[str]


class RiskManager:
    """Stateless reviewer. All persisted state comes from TrackRecord + the regime clock."""

    def __init__(
        self,
        track_record: TrackRecord,
        *,
        regime_provider: "RegimeProvider | None" = None,
        clock: "Clock | None" = None,
    ) -> None:
        self.track_record = track_record
        self.regime_provider = regime_provider or StaticRegime("neutral")
        self.clock = clock or SystemClock()
        self.settings = get_settings()

    def review(self, proposal: TradeProposal) -> Decision:
        trail: list[str] = []

        kill = self._check_kill_switch(trail)
        if kill is not None:
            return kill

        hours = self._check_market_hours(proposal, trail)
        if hours is not None:
            return hours

        regime = self._check_regime(proposal, trail)
        if regime is not None:
            return regime

        corr = self._check_correlation(proposal, trail)
        if corr is not None:
            return corr

        sized = self._size_position(proposal, trail)
        if sized.approved is False:
            return sized

        trail.append(f"APPROVED qty={sized.sized_qty}")
        return Decision(approved=True, sized_qty=sized.sized_qty, reason="all checks passed", rule_trail=trail)

    # --------------------------------------------------------------- rules

    def _check_kill_switch(self, trail: list[str]) -> Decision | None:
        dd = self.track_record.drawdown(days=self.settings.risk.kill_switch_window_days)
        if dd >= self.settings.risk.kill_switch_drawdown:
            trail.append(f"KILL_SWITCH dd={dd:.3f}>=limit={self.settings.risk.kill_switch_drawdown}")
            return Decision(
                approved=False,
                sized_qty=0.0,
                reason=f"kill switch active: drawdown {dd:.2%} exceeds {self.settings.risk.kill_switch_drawdown:.0%}",
                rule_trail=trail,
            )
        trail.append(f"kill_switch dd={dd:.3f} ok")
        return None

    def _check_market_hours(self, p: TradeProposal, trail: list[str]) -> Decision | None:
        if p.market == "crypto":
            trail.append("market_hours crypto=24/7")
            return None
        if p.horizon == "swing":
            trail.append("market_hours india_swing allowed_anytime_during_session")
            # we still gate swing to be inside the session window for entry
        now_ist = self.clock.now(ZoneInfo("Asia/Kolkata"))
        open_t = _parse_hhmm(self.settings.risk.indian_intraday_open)
        close_t = _parse_hhmm(self.settings.risk.indian_intraday_close)
        if not (open_t <= now_ist.time() <= close_t):
            trail.append(f"MARKET_HOURS india_closed at {now_ist.time().isoformat()}")
            return Decision(
                approved=False,
                sized_qty=0.0,
                reason=f"Indian market window is {self.settings.risk.indian_intraday_open}-{self.settings.risk.indian_intraday_close} IST",
                rule_trail=trail,
            )
        trail.append(f"market_hours india_open at {now_ist.time().isoformat()}")
        return None

    def _check_regime(self, p: TradeProposal, trail: list[str]) -> Decision | None:
        regime = self.regime_provider.current()
        trail.append(f"regime={regime}")
        if p.market != "crypto":
            return None
        if regime != "risk_off":
            return None
        # In risk_off, only delta-neutral agents (funding arb) may open new positions.
        if p.agent == "trading_funding":
            trail.append("regime risk_off but funding_arb is delta-neutral, allowed")
            return None
        trail.append("REGIME_BLOCK risk_off blocks directional crypto agent")
        return Decision(
            approved=False,
            sized_qty=0.0,
            reason="regime=risk_off blocks new directional crypto positions",
            rule_trail=trail,
        )

    def _check_correlation(self, p: TradeProposal, trail: list[str]) -> Decision | None:
        if p.side.upper() not in {"BUY", "LONG"}:
            trail.append("correlation skipped: not a long")
            return None
        open_longs = [
            t for t in self.track_record.open_positions() if t.side.upper() in {"BUY", "LONG"}
        ]
        # Duplicate-position guard: same ticker, same direction.
        for t in open_longs:
            if t.ticker == p.ticker:
                trail.append(f"CORRELATION duplicate_long ticker={p.ticker} agent={t.agent}")
                return Decision(
                    approved=False,
                    sized_qty=0.0,
                    reason=f"ticker {p.ticker} already held long by agent {t.agent}",
                    rule_trail=trail,
                )
        # Correlated-cluster guard.
        threshold = self.settings.risk.correlation_threshold
        max_cluster = self.settings.risk.max_correlated_longs
        correlated_count = sum(
            1
            for t in open_longs
            if p.correlation_with_open_longs.get(t.ticker, 0.0) >= threshold
        )
        # +1 for the proposed trade itself.
        if correlated_count + 1 > max_cluster:
            trail.append(
                f"CORRELATION cluster_too_big correlated={correlated_count} threshold={threshold}"
            )
            return Decision(
                approved=False,
                sized_qty=0.0,
                reason=f"{correlated_count + 1} correlated longs > cap {max_cluster}",
                rule_trail=trail,
            )
        trail.append(f"correlation ok (correlated_count={correlated_count})")
        return None

    def _size_position(self, p: TradeProposal, trail: list[str]) -> Decision:
        risk = self.settings.risk
        if p.portfolio_value <= 0:
            trail.append("SIZING portfolio_value<=0")
            return Decision(approved=False, sized_qty=0.0, reason="portfolio_value must be > 0", rule_trail=trail)
        if p.reference_price <= 0:
            trail.append("SIZING reference_price<=0")
            return Decision(approved=False, sized_qty=0.0, reason="reference_price must be > 0", rule_trail=trail)

        # Hard cap: 2% of portfolio in notional.
        hard_cap_notional = risk.max_pct_per_trade * p.portfolio_value

        # Half-Kelly fraction from this agent's recent stats.
        stats = self.track_record.agent_stats(p.agent, lookback=risk.kelly_lookback_trades)
        kelly_fraction = _half_kelly(stats.win_rate, stats.avg_win, stats.avg_loss, fraction=risk.kelly_fraction)

        # If we have no history, fall back to a conservative 1% (half the hard cap).
        if stats.trades_counted < 10:
            kelly_notional = 0.5 * hard_cap_notional
            trail.append(f"sizing cold_start kelly_notional={kelly_notional:.2f}")
        else:
            kelly_notional = kelly_fraction * p.portfolio_value
            trail.append(
                f"sizing kelly_fraction={kelly_fraction:.4f} from "
                f"wr={stats.win_rate:.3f} aw={stats.avg_win:.2f} al={stats.avg_loss:.2f}"
            )

        # Intended notional from the caller.
        intended_notional = p.intended_qty * p.reference_price

        final_notional = min(intended_notional, kelly_notional, hard_cap_notional)
        if final_notional <= 0:
            trail.append("SIZING zero_or_negative_notional")
            return Decision(approved=False, sized_qty=0.0, reason="sizing produced 0 qty", rule_trail=trail)

        sized_qty = final_notional / p.reference_price
        trail.append(
            f"sizing intended={intended_notional:.2f} kelly={kelly_notional:.2f} "
            f"cap={hard_cap_notional:.2f} chosen={final_notional:.2f}"
        )
        return Decision(approved=True, sized_qty=sized_qty, reason="sized", rule_trail=trail)


# ----------------------------------------------------------------- collaborators


class RegimeProvider:
    def current(self) -> Regime:
        raise NotImplementedError


class StaticRegime(RegimeProvider):
    def __init__(self, regime: Regime) -> None:
        self._r: Regime = regime

    def current(self) -> Regime:
        return self._r


class Clock:
    def now(self, tz: ZoneInfo | None = None) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self, tz: ZoneInfo | None = None) -> datetime:
        return datetime.now(tz or timezone.utc)


class FixedClock(Clock):
    """Useful for tests."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        if tz is None:
            return self._fixed
        return self._fixed.astimezone(tz)


# ----------------------------------------------------------------- math


def _half_kelly(win_rate: float, avg_win: float, avg_loss: float, *, fraction: float = 0.5) -> float:
    """Kelly = W/L - (1-W)/R where W = win rate, L = avg loss, R = avg win.

    Returns the fraction of portfolio to risk, capped at [0, 0.25].
    """
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0
    full_kelly = (win_rate / avg_loss) - ((1.0 - win_rate) / avg_win)
    if full_kelly <= 0:
        return 0.0
    return max(0.0, min(0.25, fraction * full_kelly))


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))
