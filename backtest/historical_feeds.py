"""Backtest-aware feeds.

Wrap a pre-loaded array of bars and only reveal rows whose timestamp is
<= the VirtualClock's current sim-time. This is the *only* place the
no-future-leakage rule is enforced — every agent that calls
`feed.fetch_ohlc(...)` during a backtest sees exactly what it would have
seen live at that moment.

  HistoricalIndiaFeed   matches the IndiaFeed Protocol
  HistoricalCryptoFeed  matches the CryptoFeed Protocol

News + sentiment are intentionally NOT replayed here. For backtests we
treat them as absent — `fetch_news` returns []. That's a conservative
choice: if a strategy still works without the news layer, it'll work
better with it. Don't fake-replay news, the look-ahead risk is too high.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backtest.clock import VirtualClock
from data.feeds_crypto import DatedCryptoBar, FundingPoint
from data.feeds_india import DatedBar, NewsItem, PricePoint


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- india


class HistoricalIndiaFeed:
    """Implements the IndiaFeed Protocol."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self._ohlc: dict[str, list[DatedBar]] = {}

    def load_ohlc(self, ticker: str, bars: list[DatedBar]) -> None:
        # Sort ascending so binary-search-style scans are easy.
        self._ohlc[ticker] = sorted(bars, key=lambda b: _aware(b.ts))

    # -- IndiaFeed protocol ----------------------------------------------------

    def fetch_news(self, ticker: str, *, limit: int = 20) -> list[NewsItem]:
        return []

    def fetch_latest_close(self, ticker: str) -> PricePoint | None:
        bars = self._visible(ticker)
        if not bars:
            return None
        last = bars[-1]
        return PricePoint(
            ticker=ticker,
            ts=_aware(last.ts),
            close=last.bar.close,
            volume=last.bar.volume,
        )

    def fetch_ohlc(self, ticker: str, *, days: int = 60) -> list[DatedBar]:
        return self._visible(ticker)[-days:]

    # -- internals -------------------------------------------------------------

    def _visible(self, ticker: str) -> list[DatedBar]:
        now = self.clock.now()
        bars = self._ohlc.get(ticker, ())
        # Most callers ask for "recent N" -> the slice is small at the end,
        # so a linear scan from the right is fine for daily bars.
        out: list[DatedBar] = []
        for b in bars:
            if _aware(b.ts) <= now:
                out.append(b)
            else:
                break
        return out


# ----------------------------------------------------------------- crypto


class HistoricalCryptoFeed:
    """Implements the CryptoFeed Protocol."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self._ohlc: dict[str, list[DatedCryptoBar]] = {}
        self._funding: dict[str, list[FundingPoint]] = {}

    def load_ohlc(self, symbol: str, bars: list[DatedCryptoBar]) -> None:
        self._ohlc[symbol] = sorted(bars, key=lambda b: _aware(b.ts))

    def load_funding_history(self, symbol: str, points: list[FundingPoint]) -> None:
        self._funding[symbol] = sorted(points, key=lambda p: _aware(p.funding_time))

    # -- CryptoFeed protocol ---------------------------------------------------

    def fetch_funding_rate(self, symbol: str) -> FundingPoint:
        now = self.clock.now()
        for p in reversed(self._funding.get(symbol, ())):
            if _aware(p.funding_time) <= now:
                return p
        raise KeyError(f"no funding data yet for {symbol} at {now.isoformat()}")

    def fetch_funding_history(
        self, symbol: str, since_ms: int | None = None, limit: int = 100
    ) -> list[FundingPoint]:
        now = self.clock.now()
        out = [p for p in self._funding.get(symbol, ()) if _aware(p.funding_time) <= now]
        return out[-limit:]

    def fetch_ohlc(
        self, symbol: str, *, timeframe: str = "4h", limit: int = 200
    ) -> list[DatedCryptoBar]:
        now = self.clock.now()
        out: list[DatedCryptoBar] = []
        for b in self._ohlc.get(symbol, ()):
            if _aware(b.ts) <= now:
                out.append(b)
            else:
                break
        return out[-limit:]
