"""Agent 6 — Crypto funding arb. Long spot + short perp when funding > threshold.
Stub: schema only. Implementation arrives week 5 per build plan — first live agent."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingFunding(Agent):
    name = "trading_funding"
    cadence = AgentCadence(every=timedelta(hours=8), aligned_to="binance_funding_8h")

    def run_once(self) -> None:
        raise NotImplementedError("trading_funding scheduled for week 5")
