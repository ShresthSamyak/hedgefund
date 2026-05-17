"""Agent 1 — Indian stock research. NSE/BSE + Google News + FinBERT + F&O OI.
Stub: schema only. Implementation arrives week 4 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class ResearchIndia(Agent):
    name = "research_india"
    cadence = AgentCadence(every=timedelta(minutes=15))

    def run_once(self) -> None:
        raise NotImplementedError("research_india scheduled for week 4")
