"""End-to-end tests for the approval gate and trade router.

We use:
  * NullApprovalGate (production code) for auto-approval paths.
  * FakeTransport (test-only) to exercise the TelegramApprovalGate state
    machine without hitting the network.
  * A real TrackRecord + RiskManager (in-memory SQLite) for the router tests.

Nothing here mocks the system under test — only the network boundary.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from comms.approval_gate import (
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
    NullApprovalGate,
    format_proposal_message,
)
from comms.telegram_bot import TelegramApprovalGate
from execution.trade_router import TradeRouter
from record.track_record import TrackRecord
from risk.risk_manager import (
    Decision,
    FixedClock,
    RiskManager,
    TradeProposal,
)

# ----------------------------------------------------------- approval gate tests


class FakeTransport:
    """Captures sends, lets tests inject replies."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._cb = None

    def register_reply_handler(self, cb) -> None:
        self._cb = cb

    def send(self, text: str) -> None:
        self.sent.append(text)

    def deliver(self, text: str) -> None:
        assert self._cb is not None, "no handler registered"
        self._cb(text)


def _proposal() -> TradeProposal:
    return TradeProposal(
        agent="trading_funding",
        market="crypto",
        ticker="BTC/USDT",
        side="BUY",
        horizon="swing",
        intended_qty=0.001,
        reference_price=60_000.0,
        portfolio_value=10_000.0,
        signal_payload={},
        reason_text="funding rate 0.012%",
    )


def _decision(qty: float = 0.001) -> Decision:
    return Decision(approved=True, sized_qty=qty, reason="sized", rule_trail=["ok"])


def test_null_approval_gate_always_approves() -> None:
    gate = NullApprovalGate()
    req = gate.build_request(_proposal(), _decision())
    outcome = gate.request(req)
    assert outcome.state == "approved"
    assert outcome.request_id == req.request_id


def test_format_proposal_message_contains_key_fields() -> None:
    req = NullApprovalGate().build_request(_proposal(), _decision())
    body = format_proposal_message(req)
    assert "trading_funding" in body
    assert "BTC/USDT" in body
    assert req.request_id in body
    assert "Reply 'YES" in body


def test_telegram_gate_approve() -> None:
    transport = FakeTransport()
    gate = TelegramApprovalGate(transport)
    req = gate.build_request(_proposal(), _decision(), timeout=timedelta(seconds=2))

    def replier() -> None:
        # tiny delay so request() is already waiting on the Event
        import time

        time.sleep(0.05)
        transport.deliver(f"YES {req.request_id}")

    t = threading.Thread(target=replier)
    t.start()
    outcome = gate.request(req)
    t.join()
    assert outcome.state == "approved"
    assert transport.sent and transport.sent[0].startswith("[ALPHAGRID]")


def test_telegram_gate_reject() -> None:
    transport = FakeTransport()
    gate = TelegramApprovalGate(transport)
    req = gate.build_request(_proposal(), _decision(), timeout=timedelta(seconds=2))

    def replier() -> None:
        import time

        time.sleep(0.05)
        transport.deliver(f"NO {req.request_id}")

    t = threading.Thread(target=replier)
    t.start()
    outcome = gate.request(req)
    t.join()
    assert outcome.state == "rejected"


def test_telegram_gate_times_out() -> None:
    transport = FakeTransport()
    gate = TelegramApprovalGate(transport)
    req = gate.build_request(_proposal(), _decision(), timeout=timedelta(milliseconds=50))
    outcome = gate.request(req)
    assert outcome.state == "timed_out"


def test_telegram_gate_ignores_unknown_request_id() -> None:
    transport = FakeTransport()
    gate = TelegramApprovalGate(transport)
    req = gate.build_request(_proposal(), _decision(), timeout=timedelta(milliseconds=200))

    def noise() -> None:
        import time

        time.sleep(0.02)
        transport.deliver("YES nope-not-mine")  # wrong id
        transport.deliver("not even a verb")
        transport.deliver("YES")  # missing id

    t = threading.Thread(target=noise)
    t.start()
    outcome = gate.request(req)
    t.join()
    assert outcome.state == "timed_out"


def test_telegram_gate_send_failure_rejects() -> None:
    class BrokenTransport(FakeTransport):
        def send(self, text: str) -> None:
            raise RuntimeError("network down")

    transport = BrokenTransport()
    gate = TelegramApprovalGate(transport)
    req = gate.build_request(_proposal(), _decision(), timeout=timedelta(seconds=1))
    outcome = gate.request(req)
    assert outcome.state == "rejected"
    assert "network down" in outcome.note


# ----------------------------------------------------------- trade router tests


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


def _india_clock_open() -> FixedClock:
    ist = datetime(2026, 5, 18, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    return FixedClock(ist.astimezone(timezone.utc))


def _india_proposal(**overrides: Any) -> TradeProposal:
    base = TradeProposal(
        agent="trading_momentum",
        market="india",
        ticker="HDFCBANK",
        side="BUY",
        horizon="intraday",
        intended_qty=10.0,
        reference_price=1500.0,
        portfolio_value=10_000.0,
        signal_payload={},
        reason_text="ewma cross",
    )
    return replace(base, **overrides)


def test_router_executed_path_without_human_gate(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_open())
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    outcome = router.submit(_india_proposal())
    assert outcome.state == "executed"
    assert outcome.trade_id is not None
    # The trade should now be open in the log.
    assert any(t.id == outcome.trade_id for t in tr.open_positions())


def test_router_rejected_by_risk_on_market_hours(tr: TrackRecord) -> None:
    # 22:00 IST -> well outside Indian intraday window.
    ist_late = datetime(2026, 5, 18, 22, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    rm = RiskManager(tr, clock=FixedClock(ist_late.astimezone(timezone.utc)))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    outcome = router.submit(_india_proposal())
    assert outcome.state == "rejected_by_risk"
    assert not tr.open_positions()


def test_router_consults_gate_when_required(tr: TrackRecord) -> None:
    class RecordingGate(ApprovalGate):
        def __init__(self) -> None:
            self.calls: list[ApprovalRequest] = []

        def request(self, req: ApprovalRequest) -> ApprovalOutcome:
            self.calls.append(req)
            return ApprovalOutcome(state="approved", request_id=req.request_id)

    gate = RecordingGate()
    rm = RiskManager(tr, clock=_india_clock_open())
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=gate,
        track_record=tr,
        require_human_approval=True,
    )
    outcome = router.submit(_india_proposal())
    assert outcome.state == "executed"
    assert len(gate.calls) == 1


def test_router_rejected_by_human(tr: TrackRecord) -> None:
    class RejectingGate(ApprovalGate):
        def request(self, req: ApprovalRequest) -> ApprovalOutcome:
            return ApprovalOutcome(state="rejected", request_id=req.request_id, note="nope")

    rm = RiskManager(tr, clock=_india_clock_open())
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=RejectingGate(),
        track_record=tr,
        require_human_approval=True,
    )
    outcome = router.submit(_india_proposal())
    assert outcome.state == "rejected_by_human"
    assert outcome.reason == "nope"
    assert not tr.open_positions()


def test_router_timed_out_does_not_log_trade(tr: TrackRecord) -> None:
    class TimeoutGate(ApprovalGate):
        def request(self, req: ApprovalRequest) -> ApprovalOutcome:
            return ApprovalOutcome(state="timed_out", request_id=req.request_id, note="no reply")

    rm = RiskManager(tr, clock=_india_clock_open())
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=TimeoutGate(),
        track_record=tr,
        require_human_approval=True,
    )
    outcome = router.submit(_india_proposal())
    assert outcome.state == "timed_out"
    assert not tr.open_positions()
