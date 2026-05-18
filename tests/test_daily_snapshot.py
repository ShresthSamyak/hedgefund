"""Tests for the daily-snapshot reporter."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from record.research_log import ResearchLog, WriteSignal
from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)
from tools.daily_snapshot import build_snapshot, write_snapshot


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


@pytest.fixture
def rl() -> ResearchLog:
    return ResearchLog(db_url="sqlite:///:memory:")


def _seed_closed(
    tr: TrackRecord,
    *,
    agent: str,
    ticker: str,
    entry: float,
    exit_price: float,
    qty: float = 1.0,
    ts: datetime | None = None,
) -> None:
    base = ts or datetime.now(timezone.utc) - timedelta(hours=1)
    tid = tr.open_trade(OpenTradeRequest(
        agent=agent, market="india", ticker=ticker, side="BUY",
        qty=qty, entry_price=entry, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={}, entry_ts=base,
    ))
    tr.close_trade(CloseTradeRequest(
        trade_id=tid, exit_price=exit_price, exit_ts=base + timedelta(minutes=10),
    ))


# ----------------------------------------------------------------- build_snapshot


def test_empty_database_produces_zero_snapshot(tr, rl) -> None:
    snap = build_snapshot(tr, rl)
    assert snap.per_agent == {}
    assert snap.portfolio_pnl == 0.0
    assert snap.portfolio_trades_closed == 0
    assert snap.kill_switch_active is False


def test_per_agent_aggregation(tr, rl) -> None:
    _seed_closed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110)
    _seed_closed(tr, agent="trading_momentum", ticker="B", entry=100, exit_price=90)
    _seed_closed(tr, agent="trading_momentum", ticker="C", entry=100, exit_price=115)
    _seed_closed(tr, agent="trading_sentiment", ticker="D", entry=100, exit_price=105)

    snap = build_snapshot(tr, rl)
    mom = snap.per_agent["trading_momentum"]
    sent = snap.per_agent["trading_sentiment"]

    assert mom["trades_closed"] == 3
    assert mom["wins"] == 2
    assert mom["losses"] == 1
    assert mom["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert mom["pnl"] == pytest.approx(15.0)

    assert sent["trades_closed"] == 1
    assert sent["wins"] == 1
    assert sent["pnl"] == pytest.approx(5.0)


def test_window_filtering(tr, rl) -> None:
    # One trade inside the 24h window, one well outside.
    _seed_closed(tr, agent="trading_pairs", ticker="OLD", entry=100, exit_price=80,
                 ts=datetime.now(timezone.utc) - timedelta(days=5))
    _seed_closed(tr, agent="trading_pairs", ticker="NEW", entry=100, exit_price=110)
    snap = build_snapshot(tr, rl, window_hours=24)
    pairs = snap.per_agent.get("trading_pairs", {})
    assert pairs.get("trades_closed") == 1
    assert pairs.get("pnl") == pytest.approx(10.0)


def test_open_positions_counted(tr, rl) -> None:
    tr.open_trade(OpenTradeRequest(
        agent="trading_funding", market="crypto", ticker="BTC/USDT", side="BUY",
        qty=0.01, entry_price=60_000, portfolio_value_at_entry=10_000.0,
        reason_text="held", signal_payload={},
    ))
    snap = build_snapshot(tr, rl)
    funding = snap.per_agent["trading_funding"]
    assert funding["open_positions"] == 1
    assert funding["trades_opened"] == 1  # entry_ts defaults to now -> inside window


def test_kill_switch_flag_set_when_drawdown_high(tr, rl) -> None:
    base = datetime.now(timezone.utc) - timedelta(days=10)
    _seed_closed(tr, agent="x", ticker="P", entry=100, exit_price=200, qty=10.0, ts=base)
    _seed_closed(tr, agent="x", ticker="Q", entry=100, exit_price=98, qty=100.0,
                 ts=base + timedelta(hours=1))
    snap = build_snapshot(tr, rl, window_hours=24 * 30)
    assert snap.kill_switch_active is True
    assert snap.drawdown_30d >= 0.10


def test_last_signal_ts_picked_up(tr, rl) -> None:
    ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=0.0001, payload={}, ts=ts,
    ))
    snap = build_snapshot(tr, rl)
    # research_crypto had no closed trades, so it won't appear in per_agent
    # — that's correct. But once we add a trade for the same agent the
    # last_signal_ts should pull through.
    tr.open_trade(OpenTradeRequest(
        agent="research_crypto", market="crypto", ticker="BTC/USDT", side="BUY",
        qty=0.01, entry_price=60_000, portfolio_value_at_entry=10_000.0,
        reason_text="held", signal_payload={},
    ))
    snap = build_snapshot(tr, rl)
    rc = snap.per_agent["research_crypto"]
    assert rc["last_signal_ts"] is not None


# ----------------------------------------------------------------- write_snapshot


def test_write_snapshot_appends_jsonl_and_writes_txt(tr, rl, tmp_path: Path) -> None:
    _seed_closed(tr, agent="trading_momentum", ticker="A", entry=100, exit_price=110)
    snap = build_snapshot(tr, rl)
    jsonl_path, txt_path = write_snapshot(snap, tmp_path)
    assert jsonl_path.exists()
    assert txt_path.exists()
    line = jsonl_path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["per_agent"]["trading_momentum"]["pnl"] == pytest.approx(10.0)
    assert "AlphaGrid daily snapshot" in txt_path.read_text(encoding="utf-8")


def test_jsonl_is_append_only(tr, rl, tmp_path: Path) -> None:
    snap = build_snapshot(tr, rl)
    write_snapshot(snap, tmp_path)
    write_snapshot(snap, tmp_path)
    write_snapshot(snap, tmp_path)
    lines = (tmp_path / "snapshots.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3


def test_reports_dir_is_created_if_missing(tr, rl, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "reports"
    assert not target.exists()
    snap = build_snapshot(tr, rl)
    write_snapshot(snap, target)
    assert target.exists()
    assert (target / "snapshots.jsonl").exists()


def test_text_render_includes_kill_switch_warning(tr, rl, tmp_path: Path) -> None:
    base = datetime.now(timezone.utc) - timedelta(days=10)
    _seed_closed(tr, agent="x", ticker="P", entry=100, exit_price=200, qty=10.0, ts=base)
    _seed_closed(tr, agent="x", ticker="Q", entry=100, exit_price=98, qty=100.0,
                 ts=base + timedelta(hours=1))
    snap = build_snapshot(tr, rl, window_hours=24 * 30)
    _, txt = write_snapshot(snap, tmp_path)
    assert "KILL SWITCH ACTIVE" in txt.read_text(encoding="utf-8")
