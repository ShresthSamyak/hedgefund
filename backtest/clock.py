"""Virtual clock for backtest replay.

The agents read the wall-clock through an injected `now_fn` callable. In
production that's `datetime.now(timezone.utc)`; in a backtest we hand them
`VirtualClock.now`, which returns whatever sim-time the runner has set.

`set` and `advance` mutate; `now` is the read path. Threadsafe is not a
goal — the backtest runner is single-threaded by design.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class VirtualClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._t = start

    def now(self) -> datetime:
        return self._t

    def set(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._t = ts

    def advance(self, delta: timedelta) -> None:
        self._t = self._t + delta

    def __repr__(self) -> str:
        return f"VirtualClock(now={self._t.isoformat()})"
