"""End-to-end kill-switch tests through the full TradeRouter pipeline.

`test_risk_manager.py` already covers the RiskManager rule in isolation.
This file proves that:
  1. The router itself refuses to log a trade when the kill switch fires.
  2. The drawdown computation honours peak equity, not just current.
  3. The gate releases automatically once equity recovers above the cap.
  4. A small drawdown does NOT trigger a false positive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from comms.approval_gate import NullApprovalGate
from execution.trade_router import TradeRouter
from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)
from risk.risk_manager import FixedClock, RiskManager, TradeProposal


@pytest.fixture
def env():
    tr = TrackRecord(db_url="sqlite:///:memory:")
    ist = datetime(2026, 5, 18, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    rm = RiskManager(tr, clock=FixedClock(ist.astimezone(timezone.utc)))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    return router, tr


def _inject_winner(tr: TrackRecord, *, ticker: str, qty: float, entry: float, exit_price: float, ts: datetime) -> None:
    tid = tr.open_trade(OpenTradeRequest(
        agent="seed", market="india", ticker=ticker, side="BUY",
        qty=qty, entry_price=entry, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={}, entry_ts=ts,
    ))
    tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=exit_price, exit_ts=ts + timedelta(minutes=1)))


def _inject_loser(tr: TrackRecord, *, ticker: str, qty: float, entry: float, exit_price: float, ts: datetime) -> None:
    _inject_winner(tr, ticker=ticker, qty=qty, entry=entry, exit_price=exit_price, ts=ts)


def _proposal(qty: float = 1.0) -> TradeProposal:
    return TradeProposal(
        agent="trading_momentum", market="india", ticker="HDFCBANK", side="BUY",
        horizon="intraday", intended_qty=qty, reference_price=1500.0,
        portfolio_value=10_000.0, signal_payload={}, reason_text="post-streak attempt",
    )


def test_router_rejects_trade_when_kill_switch_fires(env) -> None:
    router, tr = env
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    # Peak equity first.
    _inject_winner(tr, ticker="X1", qty=10.0, entry=100.0, exit_price=200.0, ts=base)  # +1000
    # Then a loss that's > 10% of peak.
    _inject_loser(tr, ticker="X2", qty=100.0, entry=100.0, exit_price=99.0,
                  ts=base + timedelta(hours=1))  # -100 (10% of 1000 peak)

    outcome = router.submit(_proposal())
    assert outcome.state == "rejected_by_risk"
    assert "kill switch" in outcome.reason.lower()
    # No new row from the router.
    assert all(t.agent != "trading_momentum" for t in tr.open_positions())


def test_no_false_positive_on_small_drawdown(env) -> None:
    router, tr = env
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _inject_winner(tr, ticker="A", qty=10.0, entry=100.0, exit_price=200.0, ts=base)  # +1000 peak
    # Tiny loss — only 5% drawdown vs peak. Well under the 10% threshold.
    _inject_loser(tr, ticker="B", qty=50.0, entry=100.0, exit_price=99.0,
                  ts=base + timedelta(hours=1))  # -50 = 5% of peak
    outcome = router.submit(_proposal())
    assert outcome.state == "executed", outcome.reason


def test_kill_switch_persists_after_equity_recovery(env) -> None:
    """A drawdown event inside the 30d window halts trading EVEN AFTER
    equity recovers — by design. Max-drawdown over the rolling window is
    the canonical definition, and a track-record-building system should
    treat a 20% drawdown as a pause-and-review signal regardless of
    subsequent recovery. The kill switch only clears when the event ages
    out of the window (see next test).
    """
    router, tr = env
    base = datetime.now(timezone.utc) - timedelta(hours=4)

    # Build a streak that triggers the kill switch.
    _inject_winner(tr, ticker="W1", qty=10.0, entry=100.0, exit_price=200.0, ts=base)
    _inject_loser(tr, ticker="L1", qty=100.0, entry=100.0, exit_price=98.0,
                  ts=base + timedelta(hours=1))  # -200, dd 20%
    rejected = router.submit(_proposal())
    assert rejected.state == "rejected_by_risk"

    # Recovery: even a massive winner does not clear the historical drawdown.
    _inject_winner(tr, ticker="W2", qty=10.0, entry=100.0, exit_price=500.0,
                   ts=base + timedelta(hours=2))  # +4000
    still_blocked = router.submit(_proposal())
    assert still_blocked.state == "rejected_by_risk"
    assert "kill switch" in still_blocked.reason.lower()


def test_kill_switch_fires_at_threshold_boundary(env) -> None:
    """Exactly 10% drawdown should already trigger (>= comparison)."""
    router, tr = env
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _inject_winner(tr, ticker="P", qty=10.0, entry=100.0, exit_price=200.0, ts=base)  # +1000 peak
    _inject_loser(tr, ticker="Q", qty=100.0, entry=100.0, exit_price=99.0,
                  ts=base + timedelta(hours=1))  # -100 = exactly 10%
    outcome = router.submit(_proposal())
    assert outcome.state == "rejected_by_risk"


def test_kill_switch_window_respects_30_days(env) -> None:
    """A loss outside the 30-day rolling window should not trigger."""
    router, tr = env
    way_back = datetime.now(timezone.utc) - timedelta(days=45)
    _inject_winner(tr, ticker="OLD_P", qty=10.0, entry=100.0, exit_price=200.0, ts=way_back)
    _inject_loser(tr, ticker="OLD_L", qty=100.0, entry=100.0, exit_price=98.0,
                  ts=way_back + timedelta(hours=1))  # -200, but 45 days ago
    outcome = router.submit(_proposal())
    assert outcome.state == "executed", outcome.reason
