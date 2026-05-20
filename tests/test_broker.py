"""Tests for the live order-placement broker layer.

NullBroker and the ccxt translation in BybitBroker. The actual Bybit
network round-trip isn't exercised here (no creds in CI) — we mock the
ccxt `create_order` call to verify the request/response mapping.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from execution.broker import (
    BrokerError,
    BrokerFill,
    BybitBroker,
    NullBroker,
    OrderRequest,
    _parse_fill,
    _to_linear_symbol,
)

# ---------------------------------------------------------------- NullBroker


def test_null_broker_returns_synthetic_fill() -> None:
    broker = NullBroker()
    order = OrderRequest(symbol="BTC/USDT", side="BUY", qty=0.01, limit_price=60_000.0,
                         client_order_id="agent-7")
    fill = broker.place_order(order)
    assert isinstance(fill, BrokerFill)
    assert fill.symbol == "BTC/USDT"
    assert fill.filled_qty == 0.01
    assert fill.filled_price == 60_000.0
    assert fill.broker_order_id.startswith("paper-")
    assert fill.raw is not None and fill.raw.get("paper") is True


def test_null_broker_handles_missing_client_order_id() -> None:
    fill = NullBroker().place_order(OrderRequest(symbol="ETH/USDT", side="SELL", qty=1.0))
    assert fill.broker_order_id == "paper-synthetic"
    assert fill.filled_qty == 1.0


# ---------------------------------------------------------------- helpers


def test_to_linear_symbol_adds_usdt_suffix() -> None:
    assert _to_linear_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert _to_linear_symbol("ETH/USDT") == "ETH/USDT:USDT"


def test_to_linear_symbol_passthrough_when_already_linear() -> None:
    assert _to_linear_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_parse_fill_uses_ccxt_average_and_filled() -> None:
    order = OrderRequest(symbol="BTC/USDT", side="BUY", qty=0.01, limit_price=60_000.0)
    raw = {
        "id": "1234567",
        "filled": "0.0098",         # ccxt sometimes returns strings
        "average": "60050.5",
        "fee": {"cost": "0.05", "currency": "USDT"},
    }
    fill = _parse_fill(order, raw)
    assert fill.broker_order_id == "1234567"
    assert fill.filled_qty == pytest.approx(0.0098)
    assert fill.filled_price == pytest.approx(60050.5)
    assert fill.fee == pytest.approx(0.05)


def test_parse_fill_falls_back_to_order_when_response_sparse() -> None:
    order = OrderRequest(symbol="BTC/USDT", side="BUY", qty=0.01, limit_price=60_000.0)
    fill = _parse_fill(order, {"id": "x"})
    assert fill.filled_qty == 0.01           # falls back to order.qty
    assert fill.filled_price == 60_000.0     # falls back to limit_price
    assert fill.fee == 0.0


def test_parse_fill_handles_non_dict_response() -> None:
    order = OrderRequest(symbol="BTC/USDT", side="BUY", qty=0.01, limit_price=60_000.0)
    fill = _parse_fill(order, None)
    assert fill.filled_qty == 0.01
    assert fill.filled_price == 60_000.0
    assert fill.broker_order_id == ""


# ---------------------------------------------------------------- BybitBroker


def test_bybit_broker_requires_credentials() -> None:
    with pytest.raises(BrokerError, match="api_key"):
        BybitBroker(api_key="", api_secret="secret")
    with pytest.raises(BrokerError, match="api_key"):
        BybitBroker(api_key="key", api_secret="")


def test_bybit_broker_translates_order_to_ccxt_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """BybitBroker should call ccxt.bybit.create_order with the right
    symbol/side/type/params shape — linear category + orderLinkId."""
    fake_ccxt_module = MagicMock()
    fake_exchange = MagicMock()
    fake_exchange.create_order.return_value = {
        "id": "abc-123",
        "filled": 0.01,
        "average": 60_100.0,
        "fee": {"cost": 0.06},
    }
    fake_ccxt_module.bybit.return_value = fake_exchange
    monkeypatch.setitem(__import__("sys").modules, "ccxt", fake_ccxt_module)

    broker = BybitBroker(api_key="K", api_secret="S", testnet=True)
    fill = broker.place_order(OrderRequest(
        symbol="BTC/USDT", side="BUY", qty=0.01, limit_price=60_000.0,
        client_order_id="agent-9",
    ))

    fake_exchange.set_sandbox_mode.assert_called_with(True)
    call = fake_exchange.create_order.call_args
    assert call.kwargs["symbol"] == "BTC/USDT:USDT"
    assert call.kwargs["side"] == "buy"
    assert call.kwargs["type"] == "market"
    assert call.kwargs["amount"] == 0.01
    assert call.kwargs["params"] == {"category": "linear", "orderLinkId": "agent-9"}
    assert fill.broker_order_id == "abc-123"
    assert fill.filled_price == 60_100.0
    assert fill.fee == 0.06


def test_bybit_broker_translates_sell_side(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ccxt_module = MagicMock()
    fake_exchange = MagicMock()
    fake_exchange.create_order.return_value = {"id": "x"}
    fake_ccxt_module.bybit.return_value = fake_exchange
    monkeypatch.setitem(__import__("sys").modules, "ccxt", fake_ccxt_module)

    broker = BybitBroker(api_key="K", api_secret="S", testnet=False)
    broker.place_order(OrderRequest(symbol="ETH/USDT", side="SELL", qty=1.0))

    fake_exchange.set_sandbox_mode.assert_not_called()  # testnet=False
    assert fake_exchange.create_order.call_args.kwargs["side"] == "sell"


def test_bybit_broker_raises_broker_error_when_ccxt_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ccxt_module = MagicMock()
    fake_exchange = MagicMock()
    fake_exchange.create_order.side_effect = RuntimeError("insufficient balance")
    fake_ccxt_module.bybit.return_value = fake_exchange
    monkeypatch.setitem(__import__("sys").modules, "ccxt", fake_ccxt_module)

    broker = BybitBroker(api_key="K", api_secret="S")
    with pytest.raises(BrokerError, match="insufficient balance"):
        broker.place_order(OrderRequest(symbol="BTC/USDT", side="BUY", qty=0.01))
