"""Backtest orchestration.

Drives a `VirtualClock` from `start` to `end` in `step` increments. At each
step, calls `run_once()` on any agent whose `cadence.every` has elapsed
since its last invocation.

  agents share:
    - the VirtualClock as their `now_fn`
    - a single in-memory TrackRecord (the backtest trade log)
    - a single in-memory ResearchLog (so e.g. research_crypto can write
      funding records that trading_funding consumes)
    - a RiskManager whose internal clock is the same VirtualClock
    - a TradeRouter with require_human_approval=False

The runner does not subscribe to news / WebSockets — backtests use only
the historical OHLC + funding feeds. The InMemoryBus is still created so
the regime gate can write its `crypto_size_modifier` to the research log
and trading_funding/trend can read it on their tick.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from agents.base import Agent
from backtest.clock import VirtualClock
from comms.approval_gate import NullApprovalGate
from execution.trade_router import TradeRouter
from infra.signal_bus import InMemoryBus
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from risk.risk_manager import Clock, RiskManager, StaticRegime

log = logging.getLogger("alphagrid.backtest")


# ---------------------------------------------------------- clock adapter

class _BacktestClock(Clock):
    """Adapter so the RiskManager's `Clock` interface points at the same
    VirtualClock the agents use.
    """

    def __init__(self, vc: VirtualClock) -> None:
        self._vc = vc

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        ts = self._vc.now()
        return ts if tz is None else ts.astimezone(tz)


# ---------------------------------------------------------- result type


@dataclass
class BacktestResult:
    start: datetime
    end: datetime
    step: timedelta
    track_record: TrackRecord
    research_log: ResearchLog
    n_ticks: int = 0
    agent_invocations: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Quick at-a-glance shape. Use `tools.weekly_report.build_report`
        on `self.track_record` for the full scorecard.
        """
        opens = self.track_record.open_positions()
        closed = self.track_record.closed_trades()
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "step_seconds": int(self.step.total_seconds()),
            "ticks": self.n_ticks,
            "agent_invocations": self.agent_invocations,
            "trades_open": len(opens),
            "trades_closed": len(closed),
            "total_pnl": round(sum((t.pnl or 0.0) for t in closed), 2),
        }


# ---------------------------------------------------------- runner


class BacktestRunner:
    def __init__(
        self,
        *,
        clock: VirtualClock,
        track_record: TrackRecord,
        research_log: ResearchLog,
        regime: str = "neutral",
    ) -> None:
        self.clock = clock
        self.track_record = track_record
        self.research_log = research_log
        self.bus = InMemoryBus(dispatch_in_thread=False)
        self.risk_manager = RiskManager(
            track_record=track_record,
            clock=_BacktestClock(clock),
            regime_provider=StaticRegime(regime),  # type: ignore[arg-type]
        )
        self.router = TradeRouter(
            risk_manager=self.risk_manager,
            approval_gate=NullApprovalGate(),
            track_record=track_record,
            require_human_approval=False,
        )

    def run(
        self,
        *,
        agents: Iterable[Agent],
        start: datetime,
        end: datetime,
        step: timedelta = timedelta(hours=1),
    ) -> BacktestResult:
        start = _aware(start)
        end = _aware(end)
        if end <= start:
            raise ValueError("end must be after start")
        self.clock.set(start)

        agents = list(agents)
        last_run: dict[str, datetime] = {a.name: start - a.cadence.every for a in agents}
        invocations: dict[str, int] = {a.name: 0 for a in agents}
        n_ticks = 0

        while self.clock.now() <= end:
            now = self.clock.now()
            n_ticks += 1
            for agent in agents:
                interval = agent.cadence.every
                if (now - last_run[agent.name]) >= interval:
                    try:
                        agent.run_once()
                        invocations[agent.name] += 1
                    except NotImplementedError:
                        pass
                    except Exception:
                        log.exception("[%s] backtest tick failed at %s", agent.name, now.isoformat())
                    last_run[agent.name] = now
            self.clock.advance(step)

        return BacktestResult(
            start=start, end=end, step=step,
            track_record=self.track_record,
            research_log=self.research_log,
            n_ticks=n_ticks,
            agent_invocations=invocations,
        )


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
