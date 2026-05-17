"""Shared base class for all 8 AlphaGrid agents.

Each agent is a small object with:
  * name — used in track_record `agent` column and config toggles
  * run_once() — one tick of work, called by the scheduler
  * cadence — how often the scheduler should call run_once
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class AgentCadence:
    every: timedelta
    aligned_to: str | None = None  # e.g. "binance_funding_8h" for explicit alignment


class Agent(ABC):
    name: str = "unknown"
    cadence: AgentCadence = AgentCadence(every=timedelta(minutes=15))

    @abstractmethod
    def run_once(self) -> None:
        """One tick. Must be idempotent — scheduler may retry on failure."""
        raise NotImplementedError
