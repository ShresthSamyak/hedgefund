"""Binance WebSocket tick stream.

Subscribes to USDT-M futures `aggTrade` streams for the configured symbols,
feeds every trade into a per-symbol `CandleBuilder`, and publishes closed
bars to the `SignalBus` on channel `price.<symbol>`.

Design notes:
  * Pure tick-handler logic is in `_handle_message` and `_handle_trade`,
    both unit-testable without a real websocket.
  * The async connection loop is intentionally simple — connect, listen,
    reconnect with exponential backoff on disconnect.
  * Bar timeframe is configurable (default 60s). Match this to the
    cadence the consuming agent needs.

References:
  * Binance docs — `https://binance-docs.github.io/apidocs/futures/en/#aggregate-trade-streams`
  * Stream URL — `wss://fstream.binance.com/stream?streams=<sym>@aggTrade/...`
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from infra.signal_bus import SignalBus
from models.candle_builder import CandleBuilder, ClosedBar

log = logging.getLogger(__name__)

FUTURES_WS_BASE = "wss://fstream.binance.com/stream"


class BinanceWebSocketStream:
    def __init__(
        self,
        *,
        symbols: Iterable[str],
        bus: SignalBus,
        timeframe_seconds: int = 60,
    ) -> None:
        self.symbols = tuple(symbols)
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        self.bus = bus
        self.timeframe_seconds = timeframe_seconds
        self._builders: dict[str, CandleBuilder] = {
            s: CandleBuilder(timeframe_seconds=timeframe_seconds) for s in self.symbols
        }
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_forever(), name="binance-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        # Flush any in-flight partial bars.
        for symbol, builder in self._builders.items():
            closed = builder.force_close()
            if closed is not None:
                self._publish(symbol, closed)

    # ------------------------------------------------------------------ connection loop

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("binance ws disconnected; reconnecting in %.1fs", backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return  # stop signaled during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2.0) + random.random()

    async def _connect_and_listen(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for live stream") from exc

        url = self._build_url(self.symbols)
        log.info("binance ws connecting %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            async for raw in ws:
                if self._stop.is_set():
                    return
                self._handle_message(raw if isinstance(raw, str) else raw.decode("utf-8"))

    # ------------------------------------------------------------------ tick handling (pure)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            log.debug("non-json ws message dropped")
            return
        data = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(data, dict):
            return
        if data.get("e") != "aggTrade":
            return
        self._handle_trade(data)

    def _handle_trade(self, trade: dict[str, Any]) -> None:
        try:
            symbol = _normalize_symbol(str(trade["s"]))
            price = float(trade["p"])
            qty = float(trade["q"])
            ts_ms = int(trade["T"])
        except (KeyError, TypeError, ValueError):
            log.debug("malformed aggTrade: %s", trade)
            return
        builder = self._builders.get(symbol)
        if builder is None:
            return
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        closed = builder.update(price, qty, ts)
        if closed is not None:
            self._publish(symbol, closed)

    def _publish(self, symbol: str, closed: ClosedBar) -> None:
        payload = {
            "symbol": symbol,
            "bar_start": closed.bar_start.isoformat(),
            "bar_end": closed.bar_end.isoformat(),
            "bar": asdict(closed.bar),
            "timeframe_seconds": self.timeframe_seconds,
        }
        self.bus.publish(f"price.{symbol}", payload)

    # ------------------------------------------------------------------ url helper

    @staticmethod
    def _build_url(symbols: tuple[str, ...]) -> str:
        # Binance expects lowercase symbols without the `/` separator.
        streams = "/".join(_to_stream(s) for s in symbols)
        return f"{FUTURES_WS_BASE}?streams={streams}"


def _to_stream(symbol: str) -> str:
    return f"{symbol.replace('/', '').lower()}@aggTrade"


def _normalize_symbol(raw: str) -> str:
    """Binance returns "BTCUSDT"; we use "BTC/USDT" internally."""
    if "/" in raw:
        return raw
    if raw.endswith("USDT"):
        return raw[:-4] + "/USDT"
    if raw.endswith("USDC"):
        return raw[:-4] + "/USDC"
    return raw
