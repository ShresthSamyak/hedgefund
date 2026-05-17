"""Agent 3 — Indian momentum. EWMA 8/32 crossover on Nifty 50, ATR sizing.
Stub: schema only. Implementation arrives week 6 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingMomentum(Agent):
    name = "trading_momentum"
    cadence = AgentCadence(every=timedelta(minutes=5))

    def run_once(self) -> None:
        raise NotImplementedError("trading_momentum scheduled for week 6")
