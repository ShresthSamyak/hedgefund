"""Agent 8 — Crypto sentiment gate. Modulates size of funding + trend agents.
Stub: schema only. Implementation arrives week 8 per build plan."""
from __future__ import annotations

from datetime import timedelta

from agents.base import Agent, AgentCadence


class TradingCryptoSent(Agent):
    name = "trading_crypto_sent"
    cadence = AgentCadence(every=timedelta(hours=4))

    def run_once(self) -> None:
        raise NotImplementedError("trading_crypto_sent scheduled for week 8")
