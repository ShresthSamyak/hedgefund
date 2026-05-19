"""Tests for the /performance/summary endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import STATE, app
from record.research_log import ResearchLog, WriteSignal
from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'perf.db'}"
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


# ----------------------------------------------------------------- shape


def test_empty_db_returns_valid_envelope(client) -> None:
    c, _, _ = client
    body = c.get("/performance/summary").json()
    # Top-level keys all present.
    for k in ("portfolio", "agents", "equity_curve", "distribution", "correlation", "trades", "ts"):
        assert k in body, f"missing {k}"
    # Portfolio defaults.
    p = body["portfolio"]
    assert p["total_pnl"] == 0.0
    assert p["total_trades"] == 0
    assert p["sharpe_30d"] is None
    assert p["annualised_return"] is None
    assert p["paper_mode"] is True
    # Empty children render as empty lists / None.
    assert body["equity_curve"] == []
    assert body["trades"] == []
    assert body["correlation"] is None
    # 8 known agents always present (status no_signal when nothing traded).
    names = {a["name"] for a in body["agents"]}
    for known in ("trading_momentum", "trading_funding", "research_india", "trading_crypto_sent"):
        assert known in names
    assert all(a["status"] == "no_signal" for a in body["agents"])


def test_aggregated_portfolio_numbers(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="trading_momentum", ticker="HDFCBANK", entry=100, exit_price=110)
    _seed(tr, agent="trading_momentum", ticker="INFY", entry=100, exit_price=95)
    _seed(tr, agent="trading_funding", ticker="BTC/USDT", entry=60_000, exit_price=60_050, qty=0.01)
    p = c.get("/performance/summary").json()["portfolio"]
    assert p["total_trades"] == 3
    assert p["total_pnl"] == pytest.approx(5.5)
    assert p["win_rate"] == pytest.approx(2 / 3, rel=1e-3)


def test_per_agent_stats(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110)
    _seed(tr, agent="trading_momentum", ticker="B", entry=100, exit_price=120)
    _seed(tr, agent="trading_momentum", ticker="C", entry=100, exit_price=90)
    body = c.get("/performance/summary").json()
    mom = next(a for a in body["agents"] if a["name"] == "trading_momentum")
    assert mom["trades"] == 3
    assert mom["wins"] == 2
    assert mom["losses"] == 1
    assert mom["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert mom["total_pnl"] == pytest.approx(20.0)
    # Sparkline matches cumulative P&L.
    assert mom["sparkline"][-1] == pytest.approx(20.0)
    assert mom["best_trade"]["pnl"] == pytest.approx(20.0)
    assert mom["worst_trade"]["pnl"] == pytest.approx(-10.0)


def test_equity_curve_shape(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110, hours_ago=10)
    _seed(tr, agent="trading_momentum", ticker="B", entry=100, exit_price=120, hours_ago=5)
    _seed(tr, agent="trading_funding", ticker="BTC", entry=60_000, exit_price=60_100, qty=0.01, hours_ago=2)
    curve = c.get("/performance/summary").json()["equity_curve"]
    assert len(curve) == 3
    # Every point has total + all agents that ever traded.
    for point in curve:
        assert "ts" in point
        assert "total" in point
        assert "trading_momentum" in point
        assert "trading_funding" in point
    # Total monotonically reflects cumulative P&L.
    assert curve[0]["total"] == pytest.approx(10.0)
    assert curve[1]["total"] == pytest.approx(30.0)
    assert curve[2]["total"] == pytest.approx(31.0)


def test_distribution_histogram_buckets(client) -> None:
    c, tr, _ = client
    for entry, exit_ in [(100, 105), (100, 110), (100, 115), (100, 95), (100, 90)]:
        _seed(tr, agent="trading_momentum", ticker=f"T{entry}-{exit_}", entry=entry, exit_price=exit_)
    d = c.get("/performance/summary").json()["distribution"]
    assert "win_loss_per_agent" in d
    assert "pnl_histogram" in d
    assert "hold_buckets" in d
    assert len(d["pnl_histogram"]) > 0
    # win/loss totals match trades.
    wl = d["win_loss_per_agent"][0]
    assert wl["agent"] == "trading_momentum"
    assert wl["wins"] + wl["losses"] == 5


def test_correlation_matrix_emerges_with_enough_data(client) -> None:
    c, tr, _ = client
    base = datetime.now(timezone.utc) - timedelta(days=7)
    # 6 distinct days of trades across 2 agents.
    for i in range(6):
        ts = base + timedelta(days=i)
        for agent, pnl_sign in (("trading_momentum", 1), ("trading_funding", -1)):
            tid = tr.open_trade(OpenTradeRequest(
                agent=agent, market="india", ticker="T", side="BUY",
                qty=1.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
                reason_text="seed", signal_payload={}, entry_ts=ts,
            ))
            tr.close_trade(CloseTradeRequest(
                trade_id=tid, exit_price=100.0 + pnl_sign * (i + 1),
                exit_ts=ts + timedelta(hours=1),
            ))
    body = c.get("/performance/summary").json()
    corr = body["correlation"]
    assert corr is not None
    assert "agents" in corr and "matrix" in corr
    assert len(corr["matrix"]) == len(corr["agents"])
    # Diagonals are 1.0
    for i in range(len(corr["matrix"])):
        assert corr["matrix"][i][i] == 1.0


def test_correlation_is_none_with_too_little_data(client) -> None:
    c, tr, _ = client
    _seed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110)
    body = c.get("/performance/summary").json()
    assert body["correlation"] is None     # < 2 agents, < 3 days


def test_trades_payload_includes_llm_reason(client) -> None:
    c, tr, _ = client
    ts = datetime.now(timezone.utc) - timedelta(hours=1)
    tid = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="A", side="BUY",
        qty=1.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
        reason_text="cross", signal_payload={"llm_reason": "Strong momentum on volume."},
        entry_ts=ts,
    ))
    tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=110.0, exit_ts=ts + timedelta(hours=2)))
    body = c.get("/performance/summary").json()
    t = next(t for t in body["trades"] if not t["open"])
    assert t["llm_reason"] == "Strong momentum on volume."
    assert t["hold_seconds"] == pytest.approx(2 * 3600, abs=5)


def test_open_trades_appear_with_open_flag(client) -> None:
    c, tr, _ = client
    tr.open_trade(OpenTradeRequest(
        agent="trading_funding", market="crypto", ticker="BTC/USDT", side="BUY",
        qty=0.01, entry_price=60_000, portfolio_value_at_entry=10_000.0,
        reason_text="held", signal_payload={},
    ))
    body = c.get("/performance/summary").json()
    opens = [t for t in body["trades"] if t["open"]]
    assert len(opens) == 1
    assert opens[0]["agent"] == "trading_funding"
    assert opens[0]["exit"] is None
    assert opens[0]["pnl"] is None
