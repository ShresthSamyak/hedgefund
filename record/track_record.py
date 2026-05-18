"""Append-only trade log — the fundraising artifact.

Every trade across every agent passes through here. The append-only guarantee
is enforced at the application layer:
  * Closed rows (exit_ts NOT NULL) are immutable. Any attempt to mutate them
    raises TrackRecordImmutableError.
  * Open rows allow exactly one transition: NULL exit fields -> filled exit fields.
  * No DELETE is ever exposed.

We start on SQLite (single file, zero infra) and migrate to PostgreSQL at
week 11 by swapping ALPHAGRID_DB_URL. The schema is portable.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from config.settings import get_settings


class TrackRecordError(Exception):
    """Base error for the trade log."""


class TrackRecordImmutableError(TrackRecordError):
    """Raised when caller tries to mutate a closed trade."""


class TradeNotFoundError(TrackRecordError):
    """Raised when caller references a trade id that does not exist."""


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(primary_key=True)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    agent: Mapped[str] = mapped_column(index=True)
    market: Mapped[str]
    ticker: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]
    qty: Mapped[float]
    entry_price: Mapped[float]
    exit_price: Mapped[float | None] = mapped_column(nullable=True)
    pnl: Mapped[float | None] = mapped_column(nullable=True)
    fees: Mapped[float] = mapped_column(default=0.0)
    portfolio_value_at_entry: Mapped[float]
    reason_text: Mapped[str]
    signal_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    paper: Mapped[int] = mapped_column(default=1)

    __table_args__ = (Index("ix_trades_agent_entry_ts", "agent", "entry_ts"),)


@dataclass(frozen=True)
class OpenTradeRequest:
    agent: str
    market: str
    ticker: str
    side: str
    qty: float
    entry_price: float
    portfolio_value_at_entry: float
    reason_text: str
    signal_payload: dict[str, Any]
    paper: bool = True
    entry_ts: datetime | None = None


@dataclass(frozen=True)
class CloseTradeRequest:
    trade_id: str
    exit_price: float
    fees: float = 0.0
    exit_ts: datetime | None = None


@dataclass(frozen=True)
class AgentStats:
    trades_counted: int
    win_rate: float
    avg_win: float
    avg_loss: float

    @classmethod
    def from_trades(cls, trades: Iterable[Trade]) -> "AgentStats":
        wins: list[float] = []
        losses: list[float] = []
        for t in trades:
            if t.pnl is None:
                continue
            if t.pnl > 0:
                wins.append(t.pnl)
            elif t.pnl < 0:
                losses.append(abs(t.pnl))
        total = len(wins) + len(losses)
        if total == 0:
            return cls(trades_counted=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0)
        return cls(
            trades_counted=total,
            win_rate=len(wins) / total,
            avg_win=(sum(wins) / len(wins)) if wins else 0.0,
            avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        )


class TrackRecord:
    """Append-only trade log facade. Inject this into every agent + the risk manager."""

    def __init__(self, db_url: str | None = None) -> None:
        settings = get_settings()
        self.db_url = db_url or settings.runtime.alphagrid_db_url
        self.engine = create_engine(self.db_url, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False, future=True)
        _install_immutability_guard()

    # ------------------------------------------------------------------ writes

    def open_trade(self, req: OpenTradeRequest) -> str:
        _validate_side(req.side)
        if req.qty <= 0:
            raise TrackRecordError(f"qty must be positive, got {req.qty}")
        if req.entry_price <= 0:
            raise TrackRecordError(f"entry_price must be positive, got {req.entry_price}")
        trade_id = str(uuid.uuid4())
        with self._Session.begin() as session:
            session.add(
                Trade(
                    id=trade_id,
                    entry_ts=req.entry_ts or _now_utc(),
                    agent=req.agent,
                    market=req.market,
                    ticker=req.ticker,
                    side=req.side.upper(),
                    qty=req.qty,
                    entry_price=req.entry_price,
                    portfolio_value_at_entry=req.portfolio_value_at_entry,
                    reason_text=req.reason_text,
                    signal_payload=_json_safe(req.signal_payload),
                    paper=1 if req.paper else 0,
                )
            )
        return trade_id

    def close_trade(self, req: CloseTradeRequest) -> Trade:
        if req.exit_price <= 0:
            raise TrackRecordError(f"exit_price must be positive, got {req.exit_price}")
        with self._Session.begin() as session:
            trade = session.get(Trade, req.trade_id)
            if trade is None:
                raise TradeNotFoundError(req.trade_id)
            if trade.exit_ts is not None:
                raise TrackRecordImmutableError(
                    f"trade {req.trade_id} is closed at {trade.exit_ts.isoformat()}; "
                    "closed trades are immutable"
                )
            trade.exit_ts = req.exit_ts or _now_utc()
            trade.exit_price = req.exit_price
            trade.fees = req.fees
            trade.pnl = _compute_pnl(trade.side, trade.qty, trade.entry_price, req.exit_price, req.fees)
            return trade

    # ------------------------------------------------------------------ reads

    def get(self, trade_id: str) -> Trade:
        with self._Session() as session:
            trade = session.get(Trade, trade_id)
            if trade is None:
                raise TradeNotFoundError(trade_id)
            return trade

    def open_positions(self, agent: str | None = None) -> list[Trade]:
        with self._Session() as session:
            stmt = select(Trade).where(Trade.exit_ts.is_(None))
            if agent is not None:
                stmt = stmt.where(Trade.agent == agent)
            return list(session.scalars(stmt))

    def closed_trades(
        self,
        agent: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Trade]:
        with self._Session() as session:
            stmt = select(Trade).where(Trade.exit_ts.is_not(None)).order_by(Trade.exit_ts.desc())
            if agent is not None:
                stmt = stmt.where(Trade.agent == agent)
            if since is not None:
                stmt = stmt.where(Trade.exit_ts >= since)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt))

    # ------------------------------------------------------------------ metrics

    def agent_stats(self, agent: str, lookback: int = 50) -> AgentStats:
        trades = self.closed_trades(agent=agent, limit=lookback)
        return AgentStats.from_trades(trades)

    def running_sharpe(self, days: int = 30) -> float:
        since = _now_utc() - timedelta(days=days)
        trades = self.closed_trades(since=since)
        returns = _daily_returns(trades)
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std == 0:
            return 0.0
        return (mean / std) * math.sqrt(252)

    def drawdown(self, days: int = 30) -> float:
        since = _now_utc() - timedelta(days=days)
        trades = self.closed_trades(since=since)
        if not trades:
            return 0.0
        trades_sorted = sorted(trades, key=lambda t: t.exit_ts or _now_utc())
        equity = 0.0
        peak = 0.0
        worst = 0.0
        for t in trades_sorted:
            equity += t.pnl or 0.0
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak > 0 else 0.0
            worst = min(worst, dd)
        return abs(worst)

    def monthly_pdf_report(self, year: int, month: int, out_path: str) -> str:
        """Render a one-page monthly summary PDF. Idempotent — overwrites out_path.

        Sections:
          * portfolio summary (total P&L, running Sharpe, max drawdown)
          * per-agent breakdown (trades, win rate, P&L)
        """
        from calendar import monthrange

        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end_day = monthrange(year, month)[1]
        end = datetime(year, month, end_day, 23, 59, 59, tzinfo=timezone.utc)
        with self._Session() as session:
            stmt = (
                select(Trade)
                .where(Trade.exit_ts.is_not(None))
                .where(Trade.exit_ts >= start)
                .where(Trade.exit_ts <= end)
                .order_by(Trade.exit_ts)
            )
            trades = list(session.scalars(stmt))

        _render_monthly_pdf(year, month, trades, out_path)
        return out_path


# ---------------------------------------------------------------------- helpers


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_side(side: str) -> None:
    if side.upper() not in {"BUY", "SELL", "LONG", "SHORT"}:
        raise TrackRecordError(f"invalid side {side!r}")


def _compute_pnl(side: str, qty: float, entry: float, exit_: float, fees: float) -> float:
    direction = 1.0 if side.upper() in {"BUY", "LONG"} else -1.0
    return direction * (exit_ - entry) * qty - fees


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        raise TrackRecordError(f"signal_payload not JSON-serializable: {exc}") from exc
    return payload


def _daily_returns(trades: list[Trade]) -> list[float]:
    by_day: dict[str, float] = {}
    for t in trades:
        if t.pnl is None or t.portfolio_value_at_entry <= 0 or t.exit_ts is None:
            continue
        day = t.exit_ts.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + (t.pnl / t.portfolio_value_at_entry)
    return list(by_day.values())


def _render_monthly_pdf(year: int, month: int, trades: list[Trade], out_path: str) -> None:
    """Reportlab is a lazy import — the rest of the module works without it."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
        )
        from reportlab.lib import colors
    except ImportError as exc:
        raise TrackRecordError(
            "reportlab is required for monthly_pdf_report; pip install reportlab"
        ) from exc

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(out_path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm)

    total_pnl = sum((t.pnl or 0.0) for t in trades)
    win_count = sum(1 for t in trades if (t.pnl or 0.0) > 0)
    loss_count = sum(1 for t in trades if (t.pnl or 0.0) < 0)
    win_rate = (win_count / (win_count + loss_count)) if (win_count + loss_count) else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts or datetime.min.replace(tzinfo=timezone.utc)):
        equity += t.pnl or 0.0
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity - peak) / peak)

    by_agent: dict[str, list[Trade]] = {}
    for t in trades:
        by_agent.setdefault(t.agent, []).append(t)

    story = []
    story.append(Paragraph(f"<b>AlphaGrid — {year}-{month:02d}</b>", styles["Title"]))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(f"Trades closed: {len(trades)}", styles["Normal"]))
    story.append(Paragraph(f"Total P&amp;L: {total_pnl:,.2f}", styles["Normal"]))
    story.append(Paragraph(f"Win rate: {win_rate:.2%}", styles["Normal"]))
    story.append(Paragraph(f"Max drawdown (intra-month): {abs(max_dd):.2%}", styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    table_data = [["Agent", "Trades", "Wins", "Losses", "Win rate", "P&L"]]
    for agent, ts in sorted(by_agent.items()):
        wins = sum(1 for t in ts if (t.pnl or 0.0) > 0)
        losses = sum(1 for t in ts if (t.pnl or 0.0) < 0)
        wr = (wins / (wins + losses)) if (wins + losses) else 0.0
        pnl = sum((t.pnl or 0.0) for t in ts)
        table_data.append([agent, str(len(ts)), str(wins), str(losses), f"{wr:.0%}", f"{pnl:,.2f}"])

    table = Table(table_data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
    ]))
    story.append(table)
    doc.build(story)


_GUARD_INSTALLED = False


def _install_immutability_guard() -> None:
    """Session-level event hooks that block:
      * any modification to a Trade row whose persisted exit_ts is non-null
      * any DELETE statement against trades

    The guard inspects attribute history so it sees the row's *prior* state,
    not the in-memory edited one.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.base import NO_VALUE

    @event.listens_for(Session, "before_flush")
    def _guard(session, _flush_context, _instances):
        for obj in session.dirty:
            if not isinstance(obj, Trade):
                continue
            state = sa_inspect(obj)
            exit_ts_attr = state.attrs.exit_ts
            history = exit_ts_attr.history
            if history.deleted:
                prior_exit_ts = history.deleted[0]
            else:
                loaded = exit_ts_attr.loaded_value
                prior_exit_ts = None if loaded is NO_VALUE else loaded
            if prior_exit_ts is not None:
                raise TrackRecordImmutableError(
                    f"trade {obj.id} was already closed at "
                    f"{prior_exit_ts.isoformat()}; closed trades cannot be modified"
                )

    @event.listens_for(Session, "do_orm_execute")
    def _block_deletes(orm_execute_state):
        if orm_execute_state.is_delete:
            raise TrackRecordImmutableError("DELETE is not permitted on the trade log")

    _GUARD_INSTALLED = True
