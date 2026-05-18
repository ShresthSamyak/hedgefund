"""Crypto data feeds. Backed by ccxt for production, but every agent receives
the feed via a Protocol so tests inject a fake.

Binance funding-rate endpoints are public — no API key needed to read.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundingPoint:
    symbol: str
    rate: float
    funding_time: datetime
    mark_price: float | None = None


class CryptoFeed(Protocol):
    def fetch_funding_rate(self, symbol: str) -> FundingPoint: ...
    def fetch_funding_history(
        self, symbol: str, since_ms: int | None = None, limit: int = 100
    ) -> list[FundingPoint]: ...


class BinanceFeed:
    """Read-only ccxt wrapper around Binance USDT perpetuals.

    Construction is lazy on `ccxt.binance` so importing this module costs
    nothing when ccxt is missing (e.g. early in install). All network calls
    bubble exceptions — the caller (agent) decides whether to retry or skip
    a tick.
    """

    def __init__(self, *, testnet: bool = False) -> None:
        try:
            import ccxt  # local import for graceful degradation
        except ImportError as exc:
            raise RuntimeError("ccxt is required for BinanceFeed") from exc
        self._exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if testnet:
            self._exchange.set_sandbox_mode(True)

    def fetch_funding_rate(self, symbol: str) -> FundingPoint:
        raw = self._exchange.fetch_funding_rate(symbol)
        ts = raw.get("fundingTimestamp") or raw.get("timestamp") or 0
        return FundingPoint(
            symbol=symbol,
            rate=float(raw.get("fundingRate") or 0.0),
            funding_time=datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
            mark_price=float(raw["markPrice"]) if raw.get("markPrice") is not None else None,
        )

    def fetch_funding_history(
        self, symbol: str, since_ms: int | None = None, limit: int = 100
    ) -> list[FundingPoint]:
        raws = self._exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=limit)
        out: list[FundingPoint] = []
        for r in raws:
            ts = r.get("timestamp") or 0
            out.append(FundingPoint(
                symbol=symbol,
                rate=float(r.get("fundingRate") or 0.0),
                funding_time=datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
                mark_price=float(r["markPrice"]) if r.get("markPrice") is not None else None,
            ))
        return out


class StaticCryptoFeed:
    """Test feed. Returns whatever values you hand it."""

    def __init__(self, points: dict[str, FundingPoint] | None = None) -> None:
        self._points: dict[str, FundingPoint] = points or {}
        self._history: dict[str, list[FundingPoint]] = {}

    def set(self, symbol: str, rate: float, *, mark_price: float | None = None) -> None:
        self._points[symbol] = FundingPoint(
            symbol=symbol,
            rate=rate,
            funding_time=datetime.now(timezone.utc),
            mark_price=mark_price,
        )

    def set_history(self, symbol: str, points: list[FundingPoint]) -> None:
        self._history[symbol] = points

    def fetch_funding_rate(self, symbol: str) -> FundingPoint:
        if symbol not in self._points:
            raise KeyError(f"no funding point set for {symbol}")
        return self._points[symbol]

    def fetch_funding_history(
        self, symbol: str, since_ms: int | None = None, limit: int = 100
    ) -> list[FundingPoint]:
        return list(self._history.get(symbol, []))[:limit]
