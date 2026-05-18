"""Smoke test for the monthly PDF report. Generates a real PDF in a tmp dir."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from record.track_record import (
    CloseTradeRequest,
    OpenTradeRequest,
    TrackRecord,
)


@pytest.fixture
def tr() -> TrackRecord:
    return TrackRecord(db_url="sqlite:///:memory:")


def test_monthly_pdf_renders(tr: TrackRecord, tmp_path: Path) -> None:
    # 2 closed trades in May 2026 across two agents.
    may = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    a = tr.open_trade(OpenTradeRequest(
        agent="trading_momentum", market="india", ticker="HDFCBANK", side="BUY",
        qty=2.0, entry_price=1500.0, portfolio_value_at_entry=10_000.0,
        reason_text="ewma", signal_payload={}, entry_ts=may,
    ))
    tr.close_trade(CloseTradeRequest(trade_id=a, exit_price=1550.0, exit_ts=may.replace(day=16)))

    b = tr.open_trade(OpenTradeRequest(
        agent="trading_funding", market="crypto", ticker="BTC/USDT", side="BUY",
        qty=0.01, entry_price=60_000.0, portfolio_value_at_entry=10_000.0,
        reason_text="funding+", signal_payload={}, entry_ts=may,
    ))
    tr.close_trade(CloseTradeRequest(trade_id=b, exit_price=59_900.0, exit_ts=may.replace(day=17)))

    out = tmp_path / "may2026.pdf"
    result = tr.monthly_pdf_report(2026, 5, str(out))
    assert result == str(out)
    assert out.exists()
    assert out.stat().st_size > 1000  # crude sanity check that something was written


def test_monthly_pdf_empty_month_still_renders(tr: TrackRecord, tmp_path: Path) -> None:
    out = tmp_path / "jan2026.pdf"
    tr.monthly_pdf_report(2026, 1, str(out))
    assert out.exists()
