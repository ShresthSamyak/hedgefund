"""Agent 7 — Crypto trend. Triple-speed EWMA on BTC/ETH/SOL, inverse-vol sizing.
Stub: schema only. Implementation arrives week 8 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingTrend(Agent):
    name = "trading_trend"
    cadence = AgentCadence(every=timedelta(hours=1))

    def run_once(self) -> None:
        raise NotImplementedError("trading_trend scheduled for week 8")
