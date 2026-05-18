"""Agent 5 — Indian pairs arb. Weekly cointegration on configured pairs, Z-score trades.
Stub: schema only. Implementation arrives week 8 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingPairs(Agent):
    name = "trading_pairs"
    cadence = AgentCadence(every=timedelta(minutes=30))

    def run_once(self) -> None:
        raise NotImplementedError("trading_pairs scheduled for week 8")
