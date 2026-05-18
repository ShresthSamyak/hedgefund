"""Approval gate abstraction.

The trade router calls `gate.request(...)` after a proposal clears the risk
manager. The gate decides whether the trade may proceed:

  * NullApprovalGate auto-approves — used for paper-mode burn-in, tests,
    and (later) when `HUMAN_APPROVAL_REQUIRED=false`.
  * TelegramApprovalGate (in telegram_bot.py) sends a Telegram message and
    blocks until the user replies YES/NO or the timeout fires.

Both implementations conform to the same contract so the router stays
agnostic to which one is wired up.
"""
from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from risk.risk_manager import Decision, TradeProposal

log = logging.getLogger(__name__)

ApprovalState = Literal["approved", "rejected", "timed_out"]


@dataclass(frozen=True)
class ApprovalOutcome:
    state: ApprovalState
    request_id: str
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.state == "approved"


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    proposal: TradeProposal
    decision: Decision
    requested_at: datetime
    timeout: timedelta
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def expires_at(self) -> datetime:
        return self.requested_at + self.timeout


class ApprovalGate(ABC):
    """Synchronous facade. Implementations may block until the user responds."""

    default_timeout: timedelta = timedelta(minutes=10)

    def build_request(
        self,
        proposal: TradeProposal,
        decision: Decision,
        *,
        timeout: timedelta | None = None,
        extras: dict[str, str] | None = None,
    ) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=str(uuid.uuid4())[:8],
            proposal=proposal,
            decision=decision,
            requested_at=datetime.now(timezone.utc),
            timeout=timeout or self.default_timeout,
            extras=extras or {},
        )

    @abstractmethod
    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        """Block until the gate resolves (approved, rejected, or timed_out)."""
        raise NotImplementedError


class NullApprovalGate(ApprovalGate):
    """Auto-approve. Use for tests, paper-mode burn-in, and the post-week-1 flip."""

    def __init__(self, *, note: str = "auto-approved (NullApprovalGate)") -> None:
        self._note = note

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        log.debug("NullApprovalGate auto-approving %s", req.request_id)
        return ApprovalOutcome(state="approved", request_id=req.request_id, note=self._note)


def format_proposal_message(req: ApprovalRequest) -> str:
    """Human-readable Telegram body. Single source of truth so the bot and
    any future Slack/Discord adapter share the wording.
    """
    p = req.proposal
    d = req.decision
    notional = d.sized_qty * p.reference_price
    pct = (notional / p.portfolio_value * 100.0) if p.portfolio_value > 0 else 0.0
    extras = "\n".join(f"  {k}: {v}" for k, v in req.extras.items())
    return (
        f"[ALPHAGRID] Trade proposal {req.request_id}\n"
        f"Agent: {p.agent}\n"
        f"Action: {p.side} {p.ticker} ({p.market}, {p.horizon})\n"
        f"Size: {d.sized_qty:g} @ {p.reference_price:g} "
        f"= {notional:,.2f} ({pct:.2f}% of portfolio)\n"
        f"Signal: {p.reason_text}\n"
        f"Risk check: PASSED"
        f"{chr(10) + extras if extras else ''}\n"
        f"Reply 'YES {req.request_id}' to approve, 'NO {req.request_id}' to reject.\n"
        f"Expires at {req.expires_at.isoformat(timespec='seconds')}."
    )
