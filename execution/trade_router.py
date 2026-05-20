"""Single entry point every agent calls when it wants to open a trade.

Flow:
    agent
      └─> TradeRouter.submit(proposal)
            ├─> RiskManager.review(proposal)        # rules + sizing
            ├─> ApprovalGate.request(...)           # optional human gate
            ├─> LLM rationale  (optional, reasoning tier)
            ├─> Broker.place_order(...)             # live only; NullBroker in paper
            └─> TrackRecord.open_trade(...)         # append-only log

Live order placement runs through `execution.broker.Broker`. In paper mode
we use `NullBroker` so the same downstream code path runs end-to-end —
trades land in TrackRecord with synthetic fills. When paper_mode flips to
False, the wired `BybitBroker` (Indian-user-friendly venue; Binance trading
endpoints are blocked for India IPs) places real orders, and the actual
filled qty/price are what get logged.

When an `LLMClient` is provided AND the `enable_llm_summaries` toggle is
on, every APPROVED trade gets a one-sentence rationale attached to its
signal_payload under `llm_reason`. The dashboard surfaces it next to the
rule-based `reason_text`. Rejections do not call the LLM (would be too
chatty + expensive).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from comms.approval_gate import ApprovalGate
from config.settings import get_settings
from execution.broker import Broker, BrokerError, BrokerFill, NullBroker, OrderRequest
from models.llm_client import LLMClient, NullLLM
from record.track_record import OpenTradeRequest, TrackRecord
from risk.risk_manager import Decision, RiskManager, TradeProposal

log = logging.getLogger(__name__)

OutcomeState = Literal[
    "executed",
    "rejected_by_risk",
    "rejected_by_human",
    "rejected_by_broker",
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
        llm: LLMClient | None = None,
        now_fn: Callable[[], datetime] | None = None,
        broker: Broker | None = None,
    ) -> None:
        self.risk = risk_manager
        self.gate = approval_gate
        self.track_record = track_record
        self.llm: LLMClient = llm or NullLLM()
        settings = get_settings()
        self.settings = settings
        self.require_human_approval = (
            require_human_approval
            if require_human_approval is not None
            else settings.telegram.human_approval_required
        )
        self.approval_timeout = approval_timeout or timedelta(minutes=10)
        self.paper = settings.runtime.paper_mode
        # Lets the BacktestRunner inject its virtual clock so dry-run trades
        # carry sim-time entry_ts instead of wall-clock.
        self._now: Callable[[], datetime] = now_fn or (lambda: datetime.now(timezone.utc))
        # NullBroker is safe to use in paper mode; live mode requires a real
        # broker (BybitBroker) explicitly injected by main.AppContext.
        self.broker: Broker = broker or NullBroker()

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

        # Optional LLM rationale. Built on a copy of the agent's payload so
        # the agent's original dict stays untouched.
        signal_payload: dict[str, Any] = dict(proposal.signal_payload)
        if self._llm_enabled():
            rationale = self._build_rationale(proposal, decision)
            if rationale:
                signal_payload["llm_reason"] = rationale

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
                signal_payload=signal_payload,
                paper=self.paper,
                entry_ts=self._now(),
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

    # ------------------------------------------------------------------ llm

    def _llm_enabled(self) -> bool:
        return (
            self.settings.vertex.enable_llm_summaries
            and not isinstance(self.llm, NullLLM)
        )

    def _build_rationale(self, proposal: TradeProposal, decision: Decision) -> str | None:
        notional = decision.sized_qty * proposal.reference_price
        pct = (notional / proposal.portfolio_value * 100.0) if proposal.portfolio_value > 0 else 0.0
        prompt = (
            "You are a quant strategy reviewer. In ONE sentence (max 40 words), "
            "state the most important reason this trade is being placed. Be specific "
            "about the numbers and the agent's logic. No fluff, no preamble, no questions.\n\n"
            f"Agent: {proposal.agent}\n"
            f"Action: {proposal.side} {proposal.ticker} ({proposal.market}, {proposal.horizon})\n"
            f"Size: {decision.sized_qty:g} @ {proposal.reference_price:g} "
            f"= {notional:,.2f} ({pct:.2f}% of portfolio)\n"
            f"Agent reasoning: {proposal.reason_text}\n"
            f"Signal payload: {proposal.signal_payload}\n"
        )
        try:
            resp = self.llm.complete(prompt, tier="reasoning")
            text = (resp.text or "").strip()
        except Exception:
            log.exception("trade rationale LLM call failed for agent=%s", proposal.agent)
            return None
        return text or None
