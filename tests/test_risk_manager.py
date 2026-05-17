"""Risk manager rule tests. In-memory SQLite TrackRecord, FixedClock for hours,
StaticRegime for regime, no mocks anywhere.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)
from risk.risk_manager import (
    FixedClock,
    RiskManager,
    StaticRegime,
    TradeProposal,
)


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


def _india_clock_at(hh: int, mm: int) -> FixedClock:
    ist = datetime(2026, 5, 18, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata"))
    return FixedClock(ist.astimezone(timezone.utc))


_DEFAULT_PROPOSAL = TradeProposal(
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


def _proposal(**overrides: Any) -> TradeProposal:
    return replace(_DEFAULT_PROPOSAL, **overrides)


def test_hard_cap_caps_intended_notional(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    # intended notional = 10 * 1500 = 15,000, way above 2% * 10,000 = 200
    decision = rm.review(_proposal())
    assert decision.approved
    assert decision.sized_qty * 1500.0 <= 200.0 + 1e-6


def test_kill_switch_triggers_on_drawdown(tr: TrackRecord) -> None:
    # Seed a 30%+ drawdown via one big losing trade.
    tid = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="X", side="BUY",
        qty=100.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={},
    ))
    tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=50.0))  # -5000 pnl
    # Need a winner first so peak > 0, then the loser drives drawdown below the peak.
    win = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="Y", side="BUY",
        qty=10.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
        reason_text="seed", signal_payload={},
    ))
    tr.close_trade(CloseTradeRequest(trade_id=win, exit_price=110.0))  # +100 then loser later

    # Order matters: re-seed in the correct chronological order — pytest sorts by exit_ts desc.
    # Make a fresh tr to control order.
    tr2 = TrackRecord(db_url="sqlite:///:memory:")
    w = tr2.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="Y", side="BUY",
        qty=10.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
        reason_text="winner first", signal_payload={},
    ))
    tr2.close_trade(CloseTradeRequest(trade_id=w, exit_price=200.0))  # +1000 equity
    l = tr2.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="X", side="BUY",
        qty=100.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
        reason_text="loser second", signal_payload={},
    ))
    tr2.close_trade(CloseTradeRequest(trade_id=l, exit_price=99.0))  # -100 -> dd vs peak 1000 = 10%

    rm = RiskManager(tr2, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal())
    assert not decision.approved
    assert "kill switch" in decision.reason.lower()


def test_indian_market_hours_block_outside_window(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(16, 0))  # after 15:25 close
    decision = rm.review(_proposal())
    assert not decision.approved
    assert "indian market" in decision.reason.lower()


def test_indian_market_hours_open_at_10am(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal())
    assert decision.approved


def test_crypto_runs_24_7(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(3, 0))  # 3am IST
    decision = rm.review(_proposal(
        agent="trading_funding", market="crypto", ticker="BTC/USDT",
        reference_price=60_000.0, intended_qty=0.005,
    ))
    assert decision.approved


def test_regime_risk_off_blocks_crypto_trend(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(10, 0), regime_provider=StaticRegime("risk_off"))
    decision = rm.review(_proposal(
        agent="trading_trend", market="crypto", ticker="BTC/USDT",
        reference_price=60_000.0, intended_qty=0.005,
    ))
    assert not decision.approved
    assert "regime" in decision.reason.lower()


def test_regime_risk_off_allows_funding_arb(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(10, 0), regime_provider=StaticRegime("risk_off"))
    decision = rm.review(_proposal(
        agent="trading_funding", market="crypto", ticker="BTC/USDT",
        reference_price=60_000.0, intended_qty=0.005,
    ))
    assert decision.approved


def test_duplicate_long_blocked(tr: TrackRecord) -> None:
    tr.open_trade(OpenTradeRequest(
        agent="trading_sentiment", market="india", ticker="HDFCBANK", side="BUY",
        qty=1.0, entry_price=1500.0, portfolio_value_at_entry=10_000.0,
        reason_text="held", signal_payload={},
    ))
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal())  # tries to BUY HDFCBANK again
    assert not decision.approved
    assert "already held" in decision.reason.lower()


def test_correlated_cluster_blocked(tr: TrackRecord) -> None:
    # Open 3 longs already (cap is 3, so a 4th correlated long must block).
    for ticker in ("A", "B", "C"):
        tr.open_trade(OpenTradeRequest(
            agent="trading_momentum", market="india", ticker=ticker, side="BUY",
            qty=1.0, entry_price=100.0, portfolio_value_at_entry=10_000.0,
            reason_text="held", signal_payload={},
        ))
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal(
        ticker="D",
        correlation_with_open_longs={"A": 0.85, "B": 0.80, "C": 0.75},
    ))
    assert not decision.approved
    assert "correlated longs" in decision.reason.lower()


def test_short_skips_correlation(tr: TrackRecord) -> None:
    tr.open_trade(OpenTradeRequest(
        agent="trading_sentiment", market="india", ticker="HDFCBANK", side="BUY",
        qty=1.0, entry_price=1500.0, portfolio_value_at_entry=10_000.0,
        reason_text="held", signal_payload={},
    ))
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal(side="SHORT"))
    # Sizing may approve but the correlation rule shouldn't block.
    assert "correlation skipped" in " ".join(decision.rule_trail).lower()


def test_cold_start_uses_conservative_size(tr: TrackRecord) -> None:
    rm = RiskManager(tr, clock=_india_clock_at(10, 0))
    decision = rm.review(_proposal())
    assert decision.approved
    # Cold-start gives half of hard cap = 1% of portfolio.
    assert decision.sized_qty * 1500.0 == pytest.approx(0.5 * 0.02 * 10_000.0, rel=1e-6)
