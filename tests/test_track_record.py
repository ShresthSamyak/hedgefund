"""Smoke tests for the append-only trade log.

Run with: pytest tests/test_track_record.py -v
These hit a real in-memory SQLite DB, no mocks.
"""
from __future__ import annotations

import pytest

from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
    TrackRecordError,
    TrackRecordImmutableError,
)


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


def _open(
    tr: TrackRecord,
    agent: str = "trading_funding",
    ticker: str = "BTC/USDT",
    side: str = "BUY",
    qty: float = 0.01,
    entry_price: float = 60_000.0,
    portfolio: float = 10_000.0,
) -> str:
    return tr.open_trade(
        OpenTradeRequest(
            agent=agent,
            market="crypto",
            ticker=ticker,
            side=side,
            qty=qty,
            entry_price=entry_price,
            portfolio_value_at_entry=portfolio,
            reason_text="test",
            signal_payload={"k": "v"},
        )
    )


def test_open_then_close_computes_pnl_long(tr: TrackRecord) -> None:
    trade_id = _open(tr, side="BUY", qty=0.01, entry_price=60_000.0)
    tr.close_trade(CloseTradeRequest(trade_id=trade_id, exit_price=61_000.0, fees=0.5))
    trade = tr.get(trade_id)
    assert trade.pnl == pytest.approx((61_000.0 - 60_000.0) * 0.01 - 0.5)
    assert trade.exit_ts is not None


def test_open_then_close_computes_pnl_short(tr: TrackRecord) -> None:
    trade_id = _open(tr, side="SHORT", qty=0.01, entry_price=60_000.0)
    tr.close_trade(CloseTradeRequest(trade_id=trade_id, exit_price=58_000.0))
    trade = tr.get(trade_id)
    assert trade.pnl == pytest.approx((58_000.0 - 60_000.0) * -1 * 0.01)


def test_closing_twice_raises(tr: TrackRecord) -> None:
    trade_id = _open(tr)
    tr.close_trade(CloseTradeRequest(trade_id=trade_id, exit_price=61_000.0))
    with pytest.raises(TrackRecordImmutableError):
        tr.close_trade(CloseTradeRequest(trade_id=trade_id, exit_price=62_000.0))


def test_open_positions_only_returns_open(tr: TrackRecord) -> None:
    a = _open(tr, ticker="BTC/USDT")
    b = _open(tr, ticker="ETH/USDT")
    tr.close_trade(CloseTradeRequest(trade_id=a, exit_price=61_000.0))
    open_ids = {t.id for t in tr.open_positions()}
    assert open_ids == {b}


def test_invalid_qty_rejected(tr: TrackRecord) -> None:
    with pytest.raises(TrackRecordError):
        _open(tr, qty=0.0)


def test_invalid_side_rejected(tr: TrackRecord) -> None:
    with pytest.raises(TrackRecordError):
        tr.open_trade(
            OpenTradeRequest(
                agent="x",
                market="crypto",
                ticker="BTC/USDT",
                side="HOLD",
                qty=1.0,
                entry_price=100.0,
                portfolio_value_at_entry=1000.0,
                reason_text="test",
                signal_payload={},
            )
        )


def test_agent_stats_handles_empty(tr: TrackRecord) -> None:
    stats = tr.agent_stats("does_not_exist")
    assert stats.trades_counted == 0
    assert stats.win_rate == 0.0


def test_agent_stats_computes_win_rate(tr: TrackRecord) -> None:
    # 2 wins, 1 loss
    for entry, exit_ in [(100.0, 110.0), (100.0, 105.0), (100.0, 90.0)]:
        tid = _open(tr, agent="momentum", entry_price=entry, qty=1.0)
        tr.close_trade(CloseTradeRequest(trade_id=tid, exit_price=exit_))
    stats = tr.agent_stats("momentum")
    assert stats.trades_counted == 3
    assert stats.win_rate == pytest.approx(2 / 3)


def test_running_sharpe_with_no_trades_is_zero(tr: TrackRecord) -> None:
    assert tr.running_sharpe() == 0.0
