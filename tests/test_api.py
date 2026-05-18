"""FastAPI endpoint + WebSocket fan-out tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import STATE, app
from infra.signal_bus import InMemoryBus
from record.research_log import ResearchLog, WriteSignal
from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """File-backed SQLite under tmp_path. FastAPI runs sync endpoints in a
    threadpool, so `:memory:` is unsafe — each thread gets its own DB.
    Reset STATE.bus/loop between tests so a test that attaches a bus
    doesn't leak its subscribers into the next test.
    """
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    tr = TrackRecord(db_url=db_url)
    rl = ResearchLog(db_url=db_url)
    monkeypatch.setattr(STATE, "track_record", tr)
    monkeypatch.setattr(STATE, "research_log", rl)
    monkeypatch.setattr(STATE, "bus", None)
    monkeypatch.setattr(STATE, "loop", None)
    with TestClient(app) as c:
        yield c, tr, rl


def _seed(
    tr: TrackRecord,
    *,
    agent: str,
    ticker: str,
    entry: float,
    exit_price: float,
    qty: float = 1.0,
    hours_ago: float = 1.0,
) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    tid = tr.open_trade(OpenTradeRequest(
        agent=agent, market="india", ticker=ticker, side="BUY",
        qty=qty, entry_price=entry, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={}, entry_ts=ts,
    ))
    tr.close_trade(CloseTradeRequest(
        trade_id=tid, exit_price=exit_price,
        exit_ts=ts + timedelta(minutes=30),
    ))
    return tid


# ----------------------------------------------------------------- REST


def test_health(client) -> None:
    c, _, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


def test_portfolio_empty(client) -> None:
    c, _, _ = client
    body = c.get("/portfolio").json()
    assert body["pnl_today"] == 0.0
    assert body["pnl_30d"] == 0.0
    assert body["open_positions"] == 0
    assert body["kill_switch_active"] is False


def test_portfolio_with_trades(client) -> None:
    c, tr, _ = client
    # Big peak winner first so the subsequent small loss stays under the
    # 10% kill-switch threshold.
    _seed(tr, agent="m", ticker="A", entry=100, exit_price=200, qty=10, hours_ago=2)
    _seed(tr, agent="m", ticker="B", entry=100, exit_price=95, qty=1, hours_ago=1)
    body = c.get("/portfolio").json()
    assert body["pnl_today"] == pytest.approx(995.0)
    assert body["trades_closed_24h"] == 2
    assert body["kill_switch_active"] is False


def test_agents_aggregates(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110)
    _seed(tr, agent="trading_momentum", ticker="B", entry=100, exit_price=90)
    _seed(tr, agent="trading_funding", ticker="BTC", entry=60000, exit_price=60050, qty=0.01)
    body = c.get("/agents").json()
    agents = {a["name"]: a for a in body["agents"]}
    assert agents["trading_momentum"]["trades_24h"] == 2
    assert agents["trading_momentum"]["wins"] == 1
    assert agents["trading_momentum"]["losses"] == 1
    assert agents["trading_momentum"]["win_rate"] == pytest.approx(0.5)
    assert agents["trading_funding"]["trades_24h"] == 1
    assert agents["trading_funding"]["status"] == "running"


def test_trades_endpoint_limit(client) -> None:
    c, tr, _ = client
    for i in range(5):
        _seed(tr, agent="m", ticker=f"T{i}", entry=100, exit_price=101)
    body = c.get("/trades?limit=3").json()
    assert len(body["closed"]) == 3
    # Each closed trade should round-trip with the expected keys.
    sample = body["closed"][0]
    for key in ("id", "agent", "ticker", "side", "qty", "entry_price", "exit_price", "pnl", "paper"):
        assert key in sample


def test_trades_open_vs_closed_split(client) -> None:
    c, tr, _ = client
    # 2 closed, 1 still open.
    _seed(tr, agent="m", ticker="A", entry=100, exit_price=110)
    _seed(tr, agent="m", ticker="B", entry=100, exit_price=95)
    tr.open_trade(OpenTradeRequest(
        agent="m", market="india", ticker="C", side="BUY", qty=1.0,
        entry_price=100, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={},
    ))
    body = c.get("/trades").json()
    assert len(body["closed"]) == 2
    assert len(body["open"]) == 1
    assert body["open"][0]["open"] is True


def test_equity_curve(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="m", ticker="A", entry=100, exit_price=110, hours_ago=72)
    _seed(tr, agent="m", ticker="B", entry=100, exit_price=120, hours_ago=48)
    _seed(tr, agent="m", ticker="C", entry=100, exit_price=95, hours_ago=24)
    body = c.get("/equity?days=7").json()
    assert len(body["series"]) == 3
    # Cumulative pnl should monotonically reflect the trade order.
    assert body["series"][0]["cumulative_pnl"] == pytest.approx(10.0)
    assert body["series"][1]["cumulative_pnl"] == pytest.approx(30.0)
    assert body["series"][2]["cumulative_pnl"] == pytest.approx(25.0)


# ----------------------------------------------------------------- WebSocket


def test_websocket_receives_hello(client) -> None:
    c, _, _ = client
    with c.websocket_connect("/live") as ws:
        msg = ws.receive_text()
        body = json.loads(msg)
        assert body["channel"] == "hello"


def test_attach_bus_forwards_events(client) -> None:
    """When the bus publishes, every connected WS gets the payload."""
    c, _, _ = client
    bus = InMemoryBus()
    STATE.attach_bus(bus)
    try:
        with c.websocket_connect("/live") as ws:
            # Drain the hello.
            ws.receive_text()
            bus.publish("news.alert", {"ticker": "HDFCBANK", "score": 0.85})
            msg = ws.receive_text()
            body = json.loads(msg)
            assert body["channel"] == "news.alert"
            assert body["payload"]["ticker"] == "HDFCBANK"
    finally:
        bus.shutdown()
