"""Single entry point every agent calls when it wants to open a trade.

Flow:
    agent
      └─> TradeRouter.submit(proposal)
            ├─> RiskManager.review(proposal)        # rules + sizing
            ├─> ApprovalGate.request(...)           # optional human gate
            └─> TrackRecord.open_trade(...)         # append-only log

We do NOT place orders here yet. Broker wiring lands in week 5 when the
funding-arb agent ships. Until then, this is paper-mode: a successful
submission produces an open row in the trade log and returns an OUTCOME the
caller can act on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from comms.approval_gate import ApprovalGate
from config.settings import get_settings
from record.track_record import OpenTradeRequest, TrackRecord
from risk.risk_manager import Decision, RiskManager, TradeProposal

log = logging.getLogger(__name__)

OutcomeState = Literal[
    "executed",
    "rejected_by_risk",
    "rejected_by_human",
    "timed_out",
]


@dataclass(frozen=True)
class TradeOutcome:
    state: OutcomeState
    reason: str
    decision: Decision | None = None
    trade_id: str | None = None
    rule_trail: list[str] | None = None


class TradeRouter:
    """Stateless orchestrator. All persistence lives in TrackRecord."""

    def __init__(
        self,
        *,
        risk_manager: RiskManager,
        approval_gate: ApprovalGate,
        track_record: TrackRecord,
        require_human_approval: bool | None = None,
        approval_timeout: timedelta | None = None,
    ) -> None:
        self.risk = risk_manager
        self.gate = approval_gate
        self.track_record = track_record
        settings = get_settings()
        self.require_human_approval = (
            require_human_approval
            if require_human_approval is not None
            else settings.telegram.human_approval_required
        )
        self.approval_timeout = approval_timeout or timedelta(minutes=10)
        self.paper = settings.runtime.paper_mode

    def submit(self, proposal: TradeProposal) -> TradeOutcome:
        decision = self.risk.review(proposal)
        if not decision.approved:
            log.info(
                "trade rejected_by_risk agent=%s ticker=%s reason=%s",
                proposal.agent, proposal.ticker, decision.reason,
            )
            return TradeOutcome(
                state="rejected_by_risk",
                reason=decision.reason,
                decision=decision,
                rule_trail=decision.rule_trail,
            )

        if self.require_human_approval:
            req = self.gate.build_request(proposal, decision, timeout=self.approval_timeout)
            outcome = self.gate.request(req)
            if outcome.state == "rejected":
                log.info("trade rejected_by_human req=%s note=%s", req.request_id, outcome.note)
                return TradeOutcome(
                    state="rejected_by_human",
                    reason=outcome.note or "user rejected",
                    decision=decision,
                    rule_trail=decision.rule_trail,
                )
            if outcome.state == "timed_out":
                log.info("trade timed_out req=%s", req.request_id)
                return TradeOutcome(
                    state="timed_out",
                    reason=outcome.note or "approval timed out",
                    decision=decision,
                    rule_trail=decision.rule_trail,
                )

        trade_id = self.track_record.open_trade(
            OpenTradeRequest(
                agent=proposal.agent,
                market=proposal.market,
                ticker=proposal.ticker,
                side=proposal.side,
                qty=decision.sized_qty,
                entry_price=proposal.reference_price,
                portfolio_value_at_entry=proposal.portfolio_value,
                reason_text=proposal.reason_text,
                signal_payload=proposal.signal_payload,
                paper=self.paper,
            )
        )
        log.info(
            "trade executed agent=%s ticker=%s qty=%g trade_id=%s paper=%s",
            proposal.agent, proposal.ticker, decision.sized_qty, trade_id, self.paper,
        )
        return TradeOutcome(
            state="executed",
            reason="logged",
            decision=decision,
            trade_id=trade_id,
            rule_trail=decision.rule_trail,
        )
