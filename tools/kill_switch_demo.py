"""Watch the kill switch fire.

Builds an isolated in-memory DB, injects a controlled losing streak so the
30-day rolling drawdown crosses 10%, then attempts a fresh trade through
the full TradeRouter. Prints what happens at each step so the operator
can see the safety net work end-to-end.

Run:
    python -m tools.kill_switch_demo
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from comms.approval_gate import NullApprovalGate
from execution.trade_router import TradeRouter
from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)
from risk.risk_manager import RiskManager, TradeProposal


def _hr(title: str) -> None:
    print()
    print("-" * 60)
    print(title)
    print("-" * 60)


def _seed_trade(
    tr: TrackRecord,
    *,
    ticker: str,
    entry: float,
    exit_price: float,
    qty: float,
    ts: datetime,
    note: str,
) -> None:
    tid = tr.open_trade(OpenTradeRequest(
        agent="historical", market="india", ticker=ticker, side="BUY",
        qty=qty, entry_price=entry, portfolio_value_at_entry=10_000.0,
        reason_text=note, signal_payload={}, entry_ts=ts,
    ))
    tr.close_trade(CloseTradeRequest(
        trade_id=tid, exit_price=exit_price, exit_ts=ts + timedelta(minutes=1),
    ))


def _proposal() -> TradeProposal:
    return TradeProposal(
        agent="trading_momentum", market="india", ticker="HDFCBANK", side="BUY",
        horizon="intraday", intended_qty=1.0, reference_price=1500.0,
        portfolio_value=10_000.0, signal_payload={"demo": True},
        reason_text="post-streak attempt by trading_momentum",
    )


def main() -> int:
    tr = TrackRecord(db_url="sqlite:///:memory:")
    rm = RiskManager(tr)  # uses SystemClock — current time
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    base = datetime.now(timezone.utc) - timedelta(hours=3)

    _hr("Step 1 — clean slate")
    print(f"  drawdown: {tr.drawdown(days=30):.2%}  (no trades yet)")
    pre = router.submit(_proposal())
    print(f"  fresh trade -> {pre.state}: {pre.reason}")

    _hr("Step 2 — seed a peak winner of +₹1000")
    _seed_trade(tr, ticker="PEAK", entry=100.0, exit_price=200.0, qty=10.0,
                ts=base, note="peak winner")
    print(f"  drawdown: {tr.drawdown(days=30):.2%}  (peak set, no DD yet)")

    _hr("Step 3 — inject a -₹200 loss (20% drawdown vs peak)")
    _seed_trade(tr, ticker="DROP", entry=100.0, exit_price=98.0, qty=100.0,
                ts=base + timedelta(hours=1), note="big loser")
    dd = tr.drawdown(days=30)
    print(f"  drawdown: {dd:.2%}  (>= 10% kill switch threshold)")

    _hr("Step 4 — momentum agent tries to enter while kill switch is hot")
    outcome = router.submit(_proposal())
    print(f"  outcome: {outcome.state}")
    print(f"  reason : {outcome.reason}")
    if outcome.state == "rejected_by_risk" and "kill switch" in outcome.reason.lower():
        print("  [OK] kill switch fired correctly")
    else:
        print("  [FAIL] kill switch did NOT fire - investigate")
        return 1

    _hr("Step 5 — recovery: massive winner does NOT clear the historical DD")
    _seed_trade(tr, ticker="REBOUND", entry=100.0, exit_price=500.0, qty=10.0,
                ts=base + timedelta(hours=2), note="huge winner")
    print(f"  drawdown: {tr.drawdown(days=30):.2%}  (event still inside window)")
    outcome2 = router.submit(_proposal())
    print(f"  outcome: {outcome2.state}  (expected: rejected_by_risk)")
    if outcome2.state != "rejected_by_risk":
        print("  [FAIL] kill switch unexpectedly released")
        return 1
    print("  [OK] kill switch correctly persists - release only by time")

    _hr("All assertions passed — drawdown safety net is wired correctly.")
    print()
    print("Note: max-drawdown over the rolling 30d window is the canonical")
    print("kill metric. A 20% DD event halts trading until it ages out of")
    print("the window. To resume sooner, the operator must intervene")
    print("manually — this is intentional: a 20% DD demands a review pause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
