"""Unit tests for the live Binance WebSocket stream handler.

We don't open a real socket — we drive the pure tick-handler methods
directly with sample Binance JSON, verify the candle builder + signal bus
behave correctly, and confirm malformed messages don't break the loop.
"""
from __future__ import annotations

import json

import pytest

from data.live_crypto_stream import BinanceWebSocketStream, _normalize_symbol, _to_stream
from infra.signal_bus import InMemoryBus


def _agg_trade(symbol: str, price: float, qty: float, ts_ms: int) -> str:
    msg = {
        "stream": f"{symbol.lower()}@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": ts_ms,
            "s": symbol,
            "p": str(price),
            "q": str(qty),
            "T": ts_ms,
        },
    }
    return json.dumps(msg)


def test_normalize_symbol_roundtrip() -> None:
    assert _normalize_symbol("BTCUSDT") == "BTC/USDT"
    assert _normalize_symbol("ETHUSDT") == "ETH/USDT"
    assert _normalize_symbol("BTC/USDT") == "BTC/USDT"


def test_to_stream() -> None:
    assert _to_stream("BTC/USDT") == "btcusdt@aggTrade"


def test_build_url_combines_streams() -> None:
    url = BinanceWebSocketStream._build_url(("BTC/USDT", "ETH/USDT"))
    assert "btcusdt@aggTrade" in url
    assert "ethusdt@aggTrade" in url


def test_tick_handler_publishes_closed_bar_at_boundary() -> None:
    bus = InMemoryBus()
    try:
        published: list = []
        bus.subscribe("price.BTC/USDT", lambda _c, p: published.append(p))
        stream = BinanceWebSocketStream(
            symbols=["BTC/USDT"], bus=bus, timeframe_seconds=60,
        )
        # Two ticks in the same bar.
        stream._handle_message(_agg_trade("BTCUSDT", 60_000, 0.1, 1_700_000_000_000))
        stream._handle_message(_agg_trade("BTCUSDT", 60_500, 0.2, 1_700_000_030_000))
        bus.drain()
        assert published == []
        # Tick in the NEXT bar -> previous one closes and publishes.
        stream._handle_message(_agg_trade("BTCUSDT", 61_000, 0.3, 1_700_000_065_000))
        bus.drain()
        assert len(published) == 1
        bar = published[0]
        assert bar["symbol"] == "BTC/USDT"
        assert bar["bar"]["open"] == pytest.approx(60_000)
        assert bar["bar"]["high"] == pytest.approx(60_500)
        assert bar["bar"]["close"] == pytest.approx(60_500)
        assert bar["bar"]["volume"] == pytest.approx(0.3)
    finally:
        bus.shutdown()


def test_tick_handler_ignores_unknown_symbol() -> None:
    bus = InMemoryBus()
    try:
        published: list = []
        bus.subscribe("price.SOL/USDT", lambda _c, p: published.append(p))
        stream = BinanceWebSocketStream(
            symbols=["BTC/USDT"], bus=bus, timeframe_seconds=60,
        )
        stream._handle_message(_agg_trade("SOLUSDT", 100, 1, 1_700_000_000_000))
        bus.drain()
        assert published == []
    finally:
        bus.shutdown()


def test_tick_handler_ignores_non_aggtrade() -> None:
    bus = InMemoryBus()
    try:
        published: list = []
        bus.subscribe("price.BTC/USDT", lambda _c, p: published.append(p))
        stream = BinanceWebSocketStream(symbols=["BTC/USDT"], bus=bus)
        stream._handle_message(json.dumps({"data": {"e": "depthUpdate"}}))
        bus.drain()
        assert published == []
    finally:
        bus.shutdown()


def test_tick_handler_survives_malformed_json() -> None:
    bus = InMemoryBus()
    try:
        stream = BinanceWebSocketStream(symbols=["BTC/USDT"], bus=bus)
        # Should not raise.
        stream._handle_message("not-json{")
        stream._handle_message("")
        stream._handle_message(json.dumps({"data": "wrong-shape"}))
        stream._handle_message(json.dumps({"data": {"e": "aggTrade"}}))  # missing fields
    finally:
        bus.shutdown()


def test_tick_handler_drives_multiple_symbols_independently() -> None:
    bus = InMemoryBus()
    try:
        btc: list = []
        eth: list = []
        bus.subscribe("price.BTC/USDT", lambda _c, p: btc.append(p))
        bus.subscribe("price.ETH/USDT", lambda _c, p: eth.append(p))
        stream = BinanceWebSocketStream(
            symbols=["BTC/USDT", "ETH/USDT"], bus=bus, timeframe_seconds=60,
        )
        stream._handle_message(_agg_trade("BTCUSDT", 60_000, 0.1, 1_700_000_000_000))
        stream._handle_message(_agg_trade("ETHUSDT", 3_000, 1.0, 1_700_000_010_000))
        # Cross both into the next bar.
        stream._handle_message(_agg_trade("BTCUSDT", 60_500, 0.1, 1_700_000_065_000))
        stream._handle_message(_agg_trade("ETHUSDT", 3_050, 1.0, 1_700_000_065_000))
        bus.drain()
        assert len(btc) == 1 and btc[0]["bar"]["close"] == pytest.approx(60_000)
        assert len(eth) == 1 and eth[0]["bar"]["close"] == pytest.approx(3_000)
    finally:
        bus.shutdown()
