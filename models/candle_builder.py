"""Pure-function tick aggregator.

Builds OHLCV bars from a stream of `(price, volume, ts)` ticks. Closes a bar
when the tick timestamp crosses the next bar boundary aligned to UTC.

  builder = CandleBuilder(timeframe_seconds=60)
  for tick_price, tick_vol, tick_ts in stream:
      closed = builder.update(tick_price, tick_vol, tick_ts)
      if closed is not None:
          bus.publish(f"price.{symbol}", asdict(closed))

`update()` returns the just-CLOSED bar (None when the tick was rolled into
the still-open one). The in-progress partial bar is available via
`current_partial()` for dashboards.

Bars are aligned to absolute time: a 60s bar boundary lands on hh:mm:00,
not on the first tick. This matters because two builders fed the same
stream will agree on boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from models.indicators import OHLCBar


@dataclass(frozen=True)
class ClosedBar:
    bar_start: datetime
    bar_end: datetime
    bar: OHLCBar


class CandleBuilder:
    def __init__(self, timeframe_seconds: int = 60) -> None:
        if timeframe_seconds <= 0:
            raise ValueError("timeframe_seconds must be positive")
        self._tf = timeframe_seconds
        self._open_ts: datetime | None = None
        self._open_price: float = 0.0
        self._high: float = 0.0
        self._low: float = 0.0
        self._close: float = 0.0
        self._volume: float = 0.0
        self._has_data = False

    def update(self, price: float, volume: float, ts: datetime) -> ClosedBar | None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bar_start = self._floor_to_bar(ts)

        closed: ClosedBar | None = None
        if self._open_ts is None:
            self._start(bar_start, price, volume)
            return None

        if bar_start > self._open_ts:
            # roll: emit the old bar, start a new one at bar_start
            closed = self._finalize()
            self._start(bar_start, price, volume)
            return closed

        # same bar — accumulate
        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._close = price
        self._volume += max(0.0, volume)
        return None

    def force_close(self) -> ClosedBar | None:
        """For shutdown or hourly flushes — close the partial bar now."""
        if not self._has_data:
            return None
        return self._finalize()

    def current_partial(self) -> ClosedBar | None:
        """The still-open bar, for live dashboards. Returns None if no data."""
        if not self._has_data or self._open_ts is None:
            return None
        return ClosedBar(
            bar_start=self._open_ts,
            bar_end=self._open_ts.replace() + _delta(self._tf),
            bar=OHLCBar(
                open=self._open_price,
                high=self._high,
                low=self._low,
                close=self._close,
                volume=self._volume,
            ),
        )

    # ------------------------------------------------------------------ internals

    def _start(self, bar_start: datetime, price: float, volume: float) -> None:
        self._open_ts = bar_start
        self._open_price = price
        self._high = price
        self._low = price
        self._close = price
        self._volume = max(0.0, volume)
        self._has_data = True

    def _finalize(self) -> ClosedBar:
        assert self._open_ts is not None
        bar = OHLCBar(
            open=self._open_price,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        closed = ClosedBar(
            bar_start=self._open_ts,
            bar_end=self._open_ts + _delta(self._tf),
            bar=bar,
        )
        self._has_data = False
        self._open_ts = None
        return closed

    def _floor_to_bar(self, ts: datetime) -> datetime:
        epoch_sec = int(ts.timestamp())
        floored = (epoch_sec // self._tf) * self._tf
        return datetime.fromtimestamp(floored, tz=timezone.utc)


def _delta(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)
