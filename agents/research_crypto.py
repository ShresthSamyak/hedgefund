"""Agent 2 — Crypto research. Binance funding + Glassnode on-chain + social sentiment.
Stub: schema only. Implementation arrives week 4 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class ResearchCrypto(Agent):
    name = "research_crypto"
    cadence = AgentCadence(every=timedelta(hours=8), aligned_to="binance_funding_8h")

    def run_once(self) -> None:
        raise NotImplementedError("research_crypto scheduled for week 4")
