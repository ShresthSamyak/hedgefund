"""Live broker — places real orders on Bybit (Indian-user-friendly).

Binance API trading endpoints are blocked for Indian users (region/KYC
restrictions), so the order-placement path goes through Bybit. Public
market data (REST OHLC, funding, WebSocket ticks) continues to come from
Binance — those reads are unauthenticated and not region-blocked.

Paper mode short-circuits via `NullBroker`. `TradeRouter` only calls a
real broker when `settings.runtime.paper_mode is False` and a `BybitBroker`
has been wired in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = "MARKET"
    limit_price: float | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class BrokerFill:
    broker_order_id: str
    symbol: str
    side: Side
    filled_qty: float
    filled_price: float
    fee: float = 0.0
    raw: dict[str, Any] | None = None


class BrokerError(RuntimeError):
    """Broker rejected or failed to place an order. Caller maps this to a
    `rejected_by_broker` outcome — never logs the trade to TrackRecord."""


class Broker(Protocol):
    def place_order(self, order: OrderRequest) -> BrokerFill: ...


class NullBroker:
    """No-op broker for paper mode + tests. Returns a synthetic fill so the
    same downstream code path (log to TrackRecord) runs in both modes."""

    def place_order(self, order: OrderRequest) -> BrokerFill:
        return BrokerFill(
            broker_order_id="paper-" + (order.client_order_id or "synthetic"),
            symbol=order.symbol,
            side=order.side,
            filled_qty=order.qty,
            filled_price=order.limit_price or 0.0,
            fee=0.0,
            raw={"paper": True},
        )


class BybitBroker:
    """Live Bybit broker — USDT-margined perpetuals via ccxt.

    Category 'linear' = USDT-margined perp contracts. ccxt uses
    `BTC/USDT:USDT` form for linear perps; the trade router passes the
    `BTC/USDT` symbol that matches `funding_universe`, so we translate.
    """

    def __init__(self, *, api_key: str, api_secret: str, testnet: bool = True) -> None:
        if not api_key or not api_secret:
            raise BrokerError("Bybit broker requires api_key + api_secret")
        try:
            import ccxt
        except ImportError as exc:
            raise BrokerError("ccxt is required for BybitBroker") from exc
        self._exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "linear"},
        })
        if testnet:
            self._exchange.set_sandbox_mode(True)
        self.testnet = testnet

    def place_order(self, order: OrderRequest) -> BrokerFill:
        ccxt_side = "buy" if order.side == "BUY" else "sell"
        ccxt_type = "market" if order.order_type == "MARKET" else "limit"
        params: dict[str, Any] = {"category": "linear"}
        if order.client_order_id:
            params["orderLinkId"] = order.client_order_id
        symbol = _to_linear_symbol(order.symbol)
        try:
            raw = self._exchange.create_order(
                symbol=symbol,
                type=ccxt_type,
                side=ccxt_side,
                amount=order.qty,
                price=order.limit_price,
                params=params,
            )
        except Exception as exc:
            log.exception("bybit place_order failed: %s", order)
            raise BrokerError(f"bybit place_order failed: {exc}") from exc
        return _parse_fill(order, raw)


def _to_linear_symbol(symbol: str) -> str:
    """`BTC/USDT` -> `BTC/USDT:USDT` so ccxt routes to linear perps."""
    if ":" in symbol:
        return symbol
    if symbol.endswith("/USDT"):
        return f"{symbol}:USDT"
    return symbol


def _parse_fill(order: OrderRequest, raw: Any) -> BrokerFill:
    raw_dict: dict[str, Any] = raw if isinstance(raw, dict) else {}
    filled_qty = float(raw_dict.get("filled") or raw_dict.get("amount") or order.qty)
    filled_price = float(
        raw_dict.get("average")
        or raw_dict.get("price")
        or order.limit_price
        or 0.0
    )
    fee_obj = raw_dict.get("fee") or {}
    fee_cost = (
        float(fee_obj.get("cost") or 0.0)
        if isinstance(fee_obj, dict)
        else 0.0
    )
    return BrokerFill(
        broker_order_id=str(raw_dict.get("id") or raw_dict.get("orderId") or ""),
        symbol=order.symbol,
        side=order.side,
        filled_qty=filled_qty,
        filled_price=filled_price,
        fee=fee_cost,
        raw=raw_dict or None,
    )
