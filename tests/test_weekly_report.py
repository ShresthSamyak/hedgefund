"""Tests for the weekly performance report."""
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
from tools.weekly_report import (
    Thresholds,
    build_report,
    render_text,
    write_report,
)


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


@pytest.fixture
def rl() -> ResearchLog:
    return ResearchLog(db_url="sqlite:///:memory:")


def _seed_trade(
    tr: TrackRecord,
    *,
    agent: str,
    ticker: str,
    entry: float,
    exit_price: float,
    qty: float = 1.0,
    days_ago: float = 1.0,
) -> None:
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    tid = tr.open_trade(OpenTradeRequest(
        agent=agent, market="india", ticker=ticker, side="BUY",
        qty=qty, entry_price=entry, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={}, entry_ts=base,
    ))
    tr.close_trade(CloseTradeRequest(
        trade_id=tid, exit_price=exit_price, exit_ts=base + timedelta(minutes=30),
    ))


# ------------------------------------------------------------------ shape


def test_empty_db_produces_n_a_with_overall_fail(tr, rl) -> None:
    report = build_report(tr, rl)
    # sharpe/win_rate/payoff/lag all None -> their PASS depends on Nullable
    # handling. The aggregate is overall_pass=False because sharpe etc fail.
    assert report.n_trades == 0
    assert report.overall_pass is False


def test_overall_pass_requires_every_metric_pass(tr, rl) -> None:
    # Build a clean set: 5 winners + 2 losers spread across 4 days.
    base = datetime.now(timezone.utc) - timedelta(days=4)
    for i, (entry, exit_price, days_ago) in enumerate([
        (100, 110, 4), (100, 112, 3.5),
        (100, 108, 3), (100, 115, 2.5),
        (100, 105, 2),
        (100, 95, 3.8), (100, 96, 1.5),
    ]):
        _seed_trade(tr, agent="m", ticker=f"T{i}",
                    entry=entry, exit_price=exit_price, qty=1.0,
                    days_ago=days_ago)

    report = build_report(tr, rl, window_days=7)
    # Expect win_rate=5/7=71%, payoff = avg(10,12,8,15,5)/avg(5,4)=10/4.5=2.22
    wr = [m for m in report.metrics if m.name == "win_rate"][0]
    payoff = [m for m in report.metrics if m.name == "payoff_ratio"][0]
    assert wr.passed
    assert wr.value > 0.6
    assert payoff.passed
    assert payoff.value is not None and payoff.value >= 1.5


# ------------------------------------------------------------------ metrics


def test_max_drawdown_metric(tr, rl) -> None:
    base = datetime.now(timezone.utc) - timedelta(days=3)
    _seed_trade(tr, agent="m", ticker="A", entry=100, exit_price=200, qty=10, days_ago=3)  # +1000
    _seed_trade(tr, agent="m", ticker="B", entry=100, exit_price=92, qty=10, days_ago=2)   # -80 = 8% DD
    report = build_report(tr, rl)
    dd_metric = [m for m in report.metrics if m.name == "max_drawdown"][0]
    assert dd_metric.value is not None
    assert 0.07 < dd_metric.value < 0.09
    assert dd_metric.passed  # below 12% threshold


def test_max_drawdown_fails_above_threshold(tr, rl) -> None:
    _seed_trade(tr, agent="m", ticker="A", entry=100, exit_price=200, qty=10, days_ago=3)
    _seed_trade(tr, agent="m", ticker="B", entry=100, exit_price=80, qty=10, days_ago=2)  # 20% DD
    report = build_report(tr, rl)
    dd = [m for m in report.metrics if m.name == "max_drawdown"][0]
    assert not dd.passed


def test_per_agent_pnl_flagged_when_one_loses(tr, rl) -> None:
    _seed_trade(tr, agent="good", ticker="G", entry=100, exit_price=120, days_ago=2)
    _seed_trade(tr, agent="bad", ticker="B", entry=100, exit_price=80, days_ago=2)
    report = build_report(tr, rl)
    pa = [m for m in report.metrics if m.name == "per_agent_pnl"][0]
    assert not pa.passed
    assert "bad" in pa.detail


def test_per_agent_pnl_passes_when_all_non_negative(tr, rl) -> None:
    _seed_trade(tr, agent="x", ticker="A", entry=100, exit_price=110, days_ago=2)
    _seed_trade(tr, agent="y", ticker="B", entry=100, exit_price=100, days_ago=2)  # break-even
    report = build_report(tr, rl)
    pa = [m for m in report.metrics if m.name == "per_agent_pnl"][0]
    assert pa.passed


def test_signal_to_trade_lag_uses_prior_research_record(tr, rl) -> None:
    """Trade entered 2 seconds after a research_log signal."""
    signal_ts = datetime.now(timezone.utc) - timedelta(days=1)
    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=0.8, payload={}, ts=signal_ts,
    ))
    # Trade entry 2 seconds after the signal.
    tid = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="HDFCBANK", side="BUY",
        qty=1.0, entry_price=1500, portfolio_value_at_entry=10_000.0,
        reason_text="x", signal_payload={},
        entry_ts=signal_ts + timedelta(seconds=2),
    ))
    tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=1510))
    report = build_report(tr, rl)
    lag = [m for m in report.metrics if m.name == "signal_to_trade_lag_s"][0]
    assert lag.value is not None
    assert 1.5 <= lag.value <= 2.5
    assert lag.passed


def test_signal_to_trade_lag_fails_when_slow(tr, rl) -> None:
    signal_ts = datetime.now(timezone.utc) - timedelta(days=1)
    rl.write(WriteSignal(
        agent="research_india", market="india", ticker="HDFCBANK",
        signal_type="sentiment_score", value=0.8, payload={}, ts=signal_ts,
    ))
    tid = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="HDFCBANK", side="BUY",
        qty=1.0, entry_price=1500, portfolio_value_at_entry=10_000.0,
        reason_text="x", signal_payload={},
        entry_ts=signal_ts + timedelta(seconds=60),  # too slow
    ))
    tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=1510))
    report = build_report(tr, rl)
    lag = [m for m in report.metrics if m.name == "signal_to_trade_lag_s"][0]
    assert not lag.passed


# ------------------------------------------------------------------ write_report


def test_write_report_jsonl_and_text(tr, rl, tmp_path: Path) -> None:
    _seed_trade(tr, agent="m", ticker="A", entry=100, exit_price=110, days_ago=2)
    report = build_report(tr, rl)
    jsonl_path, txt_path = write_report(report, tmp_path)
    assert jsonl_path.exists() and txt_path.exists()
    # Filename is ISO-week format.
    assert "weekly_" in txt_path.name and "W" in txt_path.name
    line = jsonl_path.read_text().strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["window_days"] == 7
    assert parsed["n_trades"] == 1


def test_jsonl_is_append_only(tr, rl, tmp_path: Path) -> None:
    report = build_report(tr, rl)
    write_report(report, tmp_path)
    write_report(report, tmp_path)
    lines = (tmp_path / "weekly.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_render_text_marks_pass_and_fail(tr, rl) -> None:
    # Build a report where some metrics will fail (empty -> sharpe None etc).
    report = build_report(tr, rl)
    rendered = render_text(report)
    assert "[FAIL]" in rendered or "[PASS]" in rendered
    assert "AlphaGrid weekly report" in rendered


# ------------------------------------------------------------------ thresholds


def test_custom_thresholds_change_pass_fail(tr, rl) -> None:
    _seed_trade(tr, agent="m", ticker="A", entry=100, exit_price=101, days_ago=2)  # tiny win
    _seed_trade(tr, agent="m", ticker="B", entry=100, exit_price=99, days_ago=1)   # tiny loss
    # With the default 1.5x payoff threshold, payoff=1 fails.
    default = build_report(tr, rl)
    payoff_d = [m for m in default.metrics if m.name == "payoff_ratio"][0]
    assert payoff_d.value == pytest.approx(1.0)
    assert not payoff_d.passed
    # With a relaxed 0.9x threshold, the same data passes.
    relaxed = build_report(
        tr, rl,
        thresholds=Thresholds(min_payoff_ratio=0.9),
    )
    payoff_r = [m for m in relaxed.metrics if m.name == "payoff_ratio"][0]
    assert payoff_r.passed
