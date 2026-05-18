"""TradeRouter LLM rationale tests.

The router calls the LLM with tier='reasoning' once per APPROVED trade,
attaches the response to signal_payload['llm_reason'], and recovers
silently from LLM failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from comms.approval_gate import NullApprovalGate
from execution.trade_router import TradeRouter
from models.llm_client import LLMResponse, NullLLM
from record.track_record import TrackRecord
from risk.risk_manager import FixedClock, RiskManager, TradeProposal


class _RecordingLLM:
    def __init__(self, response: str = "Funding rate 0.012% over 3 stable windows; carry yield ~13% APR vs 0.28% fee drag.") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, tier: str = "fast"):
        self.calls.append((tier, prompt))
        return LLMResponse(text=self.response, model="rec", tier=tier,
                           in_tokens=len(prompt) // 4,
                           out_tokens=len(self.response) // 4)


def _india_clock_at(h: int, m: int) -> FixedClock:
    ist = datetime(2026, 5, 18, h, m, tzinfo=ZoneInfo("Asia/Kolkata"))
    return FixedClock(ist.astimezone(timezone.utc))


def _proposal(**overrides) -> TradeProposal:
    base = TradeProposal(
        agent="trading_funding",
        market="crypto",
        ticker="BTC/USDT",
        side="BUY",
        horizon="swing",
        intended_qty=0.001,
        reference_price=60_000.0,
        portfolio_value=10_000.0,
        signal_payload={"strategy": "funding_arb", "funding_rate": 0.00012},
        reason_text="funding 0.012% per 8h stable for 3 windows",
    )
    if overrides:
        from dataclasses import replace
        base = replace(base, **overrides)
    return base


def _build_router(*, llm, enable: bool, monkeypatch):
    tr = TrackRecord(db_url="sqlite:///:memory:")
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
        llm=llm,
    )
    monkeypatch.setattr(router.settings.vertex, "enable_llm_summaries", enable)
    return router, tr


# ----------------------------------------------------------- happy path


def test_router_attaches_rationale_when_enabled(monkeypatch) -> None:
    llm = _RecordingLLM()
    router, tr = _build_router(llm=llm, enable=True, monkeypatch=monkeypatch)
    outcome = router.submit(_proposal())
    assert outcome.state == "executed"

    # LLM called once at the reasoning tier.
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "reasoning"

    # Rationale persisted in signal_payload.
    trade = tr.get(outcome.trade_id)  # type: ignore[arg-type]
    assert trade.signal_payload["llm_reason"].startswith("Funding rate 0.012%")
    # Agent's original keys still present.
    assert trade.signal_payload["strategy"] == "funding_arb"


def test_router_prompt_includes_agent_context(monkeypatch) -> None:
    llm = _RecordingLLM()
    router, _ = _build_router(llm=llm, enable=True, monkeypatch=monkeypatch)
    router.submit(_proposal())
    prompt = llm.calls[0][1]
    assert "trading_funding" in prompt
    assert "BTC/USDT" in prompt
    assert "0.012%" in prompt or "funding_arb" in prompt


# ----------------------------------------------------------- gating


def test_router_skips_llm_when_toggle_off(monkeypatch) -> None:
    llm = _RecordingLLM()
    router, tr = _build_router(llm=llm, enable=False, monkeypatch=monkeypatch)
    outcome = router.submit(_proposal())
    assert outcome.state == "executed"
    assert llm.calls == []
    trade = tr.get(outcome.trade_id)  # type: ignore[arg-type]
    assert "llm_reason" not in trade.signal_payload


def test_router_skips_llm_when_null_client(monkeypatch) -> None:
    # Even with the toggle on, NullLLM should not trigger a call.
    router, tr = _build_router(llm=NullLLM(), enable=True, monkeypatch=monkeypatch)
    outcome = router.submit(_proposal())
    assert outcome.state == "executed"
    trade = tr.get(outcome.trade_id)  # type: ignore[arg-type]
    assert "llm_reason" not in trade.signal_payload


# ----------------------------------------------------------- failure modes


def test_router_swallows_llm_errors(monkeypatch) -> None:
    class _Broken:
        def complete(self, prompt: str, *, tier: str = "fast"):
            raise RuntimeError("vertex 500")

    router, tr = _build_router(llm=_Broken(), enable=True, monkeypatch=monkeypatch)
    outcome = router.submit(_proposal())
    # Trade still logged; rationale just missing.
    assert outcome.state == "executed"
    trade = tr.get(outcome.trade_id)  # type: ignore[arg-type]
    assert "llm_reason" not in trade.signal_payload


def test_router_does_not_call_llm_on_rejection(monkeypatch) -> None:
    llm = _RecordingLLM()
    # 22:00 IST -> outside the Indian trading window, risk manager rejects.
    tr = TrackRecord(db_url="sqlite:///:memory:")
    ist_late = datetime(2026, 5, 18, 22, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    rm = RiskManager(tr, clock=FixedClock(ist_late.astimezone(timezone.utc)))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
        llm=llm,
    )
    monkeypatch.setattr(router.settings.vertex, "enable_llm_summaries", True)
    p = _proposal(agent="trading_momentum", market="india", ticker="HDFCBANK",
                  horizon="intraday", reference_price=1500.0, intended_qty=1.0)
    outcome = router.submit(p)
    assert outcome.state == "rejected_by_risk"
    assert llm.calls == []     # no LLM call burned on a rejected trade


def test_router_truncates_empty_rationale(monkeypatch) -> None:
    class _EmptyLLM:
        def complete(self, prompt: str, *, tier: str = "fast"):
            return LLMResponse(text="   ", model="empty", tier=tier,
                               in_tokens=0, out_tokens=0)

    router, tr = _build_router(llm=_EmptyLLM(), enable=True, monkeypatch=monkeypatch)
    outcome = router.submit(_proposal())
    trade = tr.get(outcome.trade_id)  # type: ignore[arg-type]
    # Empty/whitespace response treated as missing — never written.
    assert "llm_reason" not in trade.signal_payload
