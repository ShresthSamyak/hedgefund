"""Append-only research log — the daily-accumulating knowledge base.

Distinct from `trades` (which is the audit trail of money moved). This table
stores signals emitted by research agents on every tick: sentiment scores,
funding rates, on-chain metrics, regime flags, OI prints.

Trading agents query this log to make decisions. Investors and forensic
audits read it to understand *why* a trade was placed at a given moment.

Same SQLAlchemy engine + DB file as TrackRecord, so a single `alphagrid.db`
holds both. The schema is portable to PostgreSQL via ALPHAGRID_DB_URL swap.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)

from config.settings import get_settings


class ResearchLogError(Exception):
    """Base error for the research log."""


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "research_signals"

    id: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    agent: Mapped[str] = mapped_column(index=True)
    market: Mapped[str]
    ticker: Mapped[str] = mapped_column(index=True)
    signal_type: Mapped[str] = mapped_column(index=True)
    value: Mapped[float]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_research_agent_ticker_ts", "agent", "ticker", "ts"),
        Index("ix_research_type_ticker_ts", "signal_type", "ticker", "ts"),
    )


@dataclass(frozen=True)
class WriteSignal:
    agent: str
    market: str
    ticker: str
    signal_type: str
    value: float
    payload: dict[str, Any]
    ts: datetime | None = None


class ResearchLog:
    """Append-only research signal store. Inject into every research agent."""

    def __init__(self, db_url: str | None = None) -> None:
        settings = get_settings()
        self.db_url = db_url or settings.runtime.alphagrid_db_url
        self.engine = create_engine(self.db_url, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    # --------------------------------------------------------------- write

    def write(self, signal: WriteSignal) -> str:
        if not signal.agent or not signal.signal_type or not signal.ticker:
            raise ResearchLogError("agent, ticker, signal_type are required")
        _ensure_json_safe(signal.payload)
        sid = str(uuid.uuid4())
        with self._Session.begin() as session:
            session.add(
                SignalRecord(
                    id=sid,
                    ts=signal.ts or _now_utc(),
                    agent=signal.agent,
                    market=signal.market,
                    ticker=signal.ticker,
                    signal_type=signal.signal_type,
                    value=signal.value,
                    payload=signal.payload,
                )
            )
        return sid

    def write_batch(self, signals: Iterable[WriteSignal]) -> list[str]:
        ids: list[str] = []
        with self._Session.begin() as session:
            for s in signals:
                if not s.agent or not s.signal_type or not s.ticker:
                    raise ResearchLogError("agent, ticker, signal_type are required")
                _ensure_json_safe(s.payload)
                sid = str(uuid.uuid4())
                ids.append(sid)
                session.add(
                    SignalRecord(
                        id=sid,
                        ts=s.ts or _now_utc(),
                        agent=s.agent,
                        market=s.market,
                        ticker=s.ticker,
                        signal_type=s.signal_type,
                        value=s.value,
                        payload=s.payload,
                    )
                )
        return ids

    # --------------------------------------------------------------- read

    def latest(
        self,
        ticker: str,
        signal_type: str,
        *,
        agent: str | None = None,
    ) -> SignalRecord | None:
        with self._Session() as session:
            stmt = (
                select(SignalRecord)
                .where(SignalRecord.ticker == ticker)
                .where(SignalRecord.signal_type == signal_type)
                .order_by(SignalRecord.ts.desc())
                .limit(1)
            )
            if agent is not None:
                stmt = stmt.where(SignalRecord.agent == agent)
            return session.scalars(stmt).first()

    def recent(
        self,
        signal_type: str,
        *,
        window: timedelta,
        ticker: str | None = None,
        agent: str | None = None,
        limit: int | None = None,
    ) -> list[SignalRecord]:
        since = _now_utc() - window
        with self._Session() as session:
            stmt = (
                select(SignalRecord)
                .where(SignalRecord.signal_type == signal_type)
                .where(SignalRecord.ts >= since)
                .order_by(SignalRecord.ts.desc())
            )
            if ticker is not None:
                stmt = stmt.where(SignalRecord.ticker == ticker)
            if agent is not None:
                stmt = stmt.where(SignalRecord.agent == agent)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt))

    def count_since(self, signal_type: str, *, window: timedelta) -> int:
        return len(self.recent(signal_type, window=window))


# ---------------------------------------------------------------- helpers


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_json_safe(payload: dict[str, Any]) -> None:
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        raise ResearchLogError(f"payload not JSON-serializable: {exc}") from exc
