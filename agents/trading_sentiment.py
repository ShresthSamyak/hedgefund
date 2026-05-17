"""Agent 4 — Indian sentiment. Consumes research_india output.
Stub: schema only. Implementation arrives week 8 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingSentiment(Agent):
    name = "trading_sentiment"
    cadence = AgentCadence(every=timedelta(minutes=15))

    def run_once(self) -> None:
        raise NotImplementedError("trading_sentiment scheduled for week 8")
