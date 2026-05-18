"""Tests for the Telegram digest formatter."""
from __future__ import annotations

from comms.telegram_digest import (
    NARRATIVE_MAX_CHARS,
    TELEGRAM_LIMIT,
    build_narrative,
    format_digest,
    send_digest,
)
from models.llm_client import LLMResponse, NullLLM
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


# ----------------------------------------------------------------- narrative


class _RecordingLLM:
    def __init__(self, response: str = "Solid 24h: momentum led with +245, funding stable. Watch INFY drift.") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []   # (tier, prompt)

    def complete(self, prompt: str, *, tier: str = "fast"):
        self.calls.append((tier, prompt))
        return LLMResponse(text=self.response, model="rec", tier=tier,
                           in_tokens=len(prompt) // 4,
                           out_tokens=len(self.response) // 4)


def _agent_row(closed=1, wins=1, losses=0, pnl=100.0, open_=0):
    return {
        "trades_closed": closed, "wins": wins, "losses": losses,
        "win_rate": wins / max(1, wins + losses), "pnl": pnl,
        "open_positions": open_, "trades_opened": 0, "last_signal_ts": None,
    }


def test_build_narrative_returns_none_for_null_llm() -> None:
    snap = _snap(per_agent={"trading_momentum": _agent_row()})
    assert build_narrative(snap, NullLLM()) is None


def test_build_narrative_returns_none_for_no_activity() -> None:
    snap = _snap(per_agent={})
    assert build_narrative(snap, _RecordingLLM()) is None


def test_build_narrative_uses_reasoning_tier() -> None:
    snap = _snap(per_agent={"trading_momentum": _agent_row(pnl=245.0)})
    llm = _RecordingLLM()
    narrative = build_narrative(snap, llm)
    assert narrative
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "reasoning"
    assert "trading_momentum" in llm.calls[0][1]
    # Prompt should include the actual numbers so the model can be specific.
    assert "+245" in llm.calls[0][1]


def test_build_narrative_truncates_to_max_chars() -> None:
    snap = _snap(per_agent={"trading_momentum": _agent_row()})
    huge_response = "x" * (NARRATIVE_MAX_CHARS * 3)
    llm = _RecordingLLM(response=huge_response)
    narrative = build_narrative(snap, llm)
    assert narrative is not None
    assert len(narrative) <= NARRATIVE_MAX_CHARS


def test_build_narrative_swallows_llm_errors() -> None:
    class _Broken:
        def complete(self, prompt: str, *, tier: str = "fast"):
            raise RuntimeError("vertex 500")

    snap = _snap(per_agent={"trading_momentum": _agent_row()})
    assert build_narrative(snap, _Broken()) is None


def test_format_digest_prepends_narrative() -> None:
    snap = _snap(per_agent={"trading_momentum": _agent_row(pnl=245.0)})
    text = format_digest(snap, narrative="Strong day for momentum, funding flat.")
    assert "Strong day for momentum" in text
    # Narrative comes before the metrics table.
    assert text.find("Strong day") < text.find("pnl:")


def test_send_digest_passes_llm_through() -> None:
    transport = FakeTransport()
    llm = _RecordingLLM()
    snap = _snap(per_agent={"trading_momentum": _agent_row(pnl=42.0)})
    sent = send_digest(snap, transport, llm=llm)
    assert transport.sent == [sent]
    # The narrative response shows up in the rendered text.
    assert "Solid 24h" in sent
    assert llm.calls and llm.calls[0][0] == "reasoning"


def test_send_digest_without_llm_skips_narrative() -> None:
    transport = FakeTransport()
    snap = _snap(per_agent={"trading_momentum": _agent_row(pnl=42.0)})
    sent = send_digest(snap, transport)
    assert "Solid 24h" not in sent          # no narrative present
    assert "trading_momentum" in sent       # but the table is still there
