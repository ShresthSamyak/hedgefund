"""Tests for the Telegram digest formatter."""
from __future__ import annotations

from comms.telegram_digest import TELEGRAM_LIMIT, format_digest, send_digest
from tools.daily_snapshot import Snapshot


def _snap(**overrides) -> Snapshot:
    defaults = dict(
        snapshot_ts="2026-05-19T06:00:00+00:00",
        window_hours=24,
        per_agent={},
        portfolio_pnl=0.0,
        portfolio_trades_closed=0,
        running_sharpe_30d=0.0,
        drawdown_30d=0.0,
        kill_switch_active=False,
        paper_mode=True,
    )
    defaults.update(overrides)
    return Snapshot(**defaults)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_formats_empty_snapshot() -> None:
    text = format_digest(_snap())
    assert "AlphaGrid digest" in text
    assert "mode=PAPER" in text
    assert "no agent activity in window" in text


def test_live_mode_label() -> None:
    text = format_digest(_snap(paper_mode=False))
    assert "mode=LIVE" in text


def test_per_agent_rendered() -> None:
    text = format_digest(_snap(
        portfolio_pnl=312.5,
        portfolio_trades_closed=4,
        per_agent={
            "trading_momentum": {
                "trades_closed": 3, "wins": 2, "losses": 1,
                "win_rate": 2 / 3, "pnl": 245.0, "open_positions": 0,
                "trades_opened": 0, "last_signal_ts": None,
            },
            "trading_funding": {
                "trades_closed": 1, "wins": 1, "losses": 0,
                "win_rate": 1.0, "pnl": 67.5, "open_positions": 1,
                "trades_opened": 0, "last_signal_ts": None,
            },
        },
    ))
    assert "trading_momentum" in text
    assert "trading_funding" in text
    assert "pnl: +312.50" in text
    assert "wr=67%" in text


def test_kill_switch_banner() -> None:
    text = format_digest(_snap(kill_switch_active=True, drawdown_30d=0.18))
    assert "KILL SWITCH ACTIVE" in text


def test_negative_pnl_no_plus_sign() -> None:
    text = format_digest(_snap(portfolio_pnl=-42.0))
    assert "pnl: -42.00" in text


def test_truncates_when_over_4096_chars() -> None:
    huge = {
        f"agent_{i}": {
            "trades_closed": i, "wins": i, "losses": 0,
            "win_rate": 1.0, "pnl": 0.01 * i, "open_positions": 0,
            "trades_opened": 0, "last_signal_ts": None,
        }
        for i in range(200)
    }
    text = format_digest(_snap(per_agent=huge))
    assert len(text) <= TELEGRAM_LIMIT
    assert "(truncated" in text


def test_send_digest_uses_transport() -> None:
    transport = FakeTransport()
    sent = send_digest(_snap(portfolio_pnl=10.0), transport)
    assert transport.sent == [sent]
    assert "pnl: +10.00" in sent
