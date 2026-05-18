"""End-to-end tests for the funding-arb agent.

Rates are expressed as fractions matching ccxt (0.0001 = 0.01% per 8h).
Real ResearchLog + TrackRecord + RiskManager + TradeRouter (in-memory SQLite),
NullApprovalGate, FixedClock. No mocks — only the live ccxt feed is bypassed
because the agent reads funding history directly from research_log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.trading_funding import TradingFunding
from comms.approval_gate import NullApprovalGate
from execution.trade_router import TradeRouter
from record.research_log import ResearchLog, WriteSignal
from record.track_record import CloseTradeRequest, TrackRecord
from risk.risk_manager import FixedClock, RiskManager, StaticRegime


@pytest.fixture
def env():
    rl = ResearchLog(db_url="sqlite:///:memory:")
    tr = TrackRecord(db_url="sqlite:///:memory:")
    clock = FixedClock(datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc))
    rm = RiskManager(tr, clock=clock, regime_provider=StaticRegime("neutral"))
    router = TradeRouter(
        risk_manager=rm,
        approval_gate=NullApprovalGate(),
        track_record=tr,
        require_human_approval=False,
    )
    agent = TradingFunding(
        research_log=rl,
        track_record=tr,
        trade_router=router,
        portfolio_value_getter=lambda: 10_000.0,
    )
    return agent, rl, tr


def _seed_funding(
    rl: ResearchLog,
    symbol: str,
    rates: list[float],
    *,
    mark_price: float = 60_000.0,
    base_ts: datetime | None = None,
) -> None:
    """Write `rates` in chronological order — index 0 is the OLDEST.

    Rates are fractions (0.00012 = 0.012% per 8h).
    """
    base = base_ts or datetime.now(timezone.utc) - timedelta(hours=8 * len(rates))
    for i, rate in enumerate(rates):
        ts = base + timedelta(hours=8 * i)
        rl.write(WriteSignal(
            agent="research_crypto",
            market="crypto",
            ticker=symbol,
            signal_type="funding_rate",
            value=rate,
            payload={"mark_price": mark_price, "funding_time": ts.isoformat()},
            ts=ts,
        ))


# --------------------------------------------------------------- entry tests


def test_enters_when_funding_stable_above_threshold(env) -> None:
    agent, rl, tr = env
    # 0.012% per 8h, stable for 3 prints — clean entry signal.
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    open_pos = [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT"]
    assert len(open_pos) == 1
    pos = open_pos[0]
    assert pos.signal_payload["strategy"] == "funding_arb"
    assert pos.signal_payload["funding_rate"] == pytest.approx(0.00012)


def test_does_not_enter_on_single_spike(env) -> None:
    agent, rl, tr = env
    # Only the latest is above threshold; two prior below.
    _seed_funding(rl, "BTC/USDT", [0.00005, 0.00006, 0.00020])
    agent.run_once()
    assert not [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT"]


def test_does_not_enter_when_not_enough_history(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013])  # only 2 prints, need 3
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


def test_does_not_enter_when_decaying(env) -> None:
    agent, rl, tr = env
    # All above threshold but latest is 50% of median -> decay floor 0.8 triggers.
    _seed_funding(rl, "BTC/USDT", [0.00025, 0.00018, 0.00011])
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


def test_does_not_enter_when_no_mark_price(env) -> None:
    agent, rl, tr = env
    for _ in range(3):
        rl.write(WriteSignal(
            agent="research_crypto", market="crypto", ticker="BTC/USDT",
            signal_type="funding_rate", value=0.00012, payload={},
        ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


def test_tier_sizing_higher_funding_bigger_size(env) -> None:
    agent, rl, tr = env
    # BTC at 0.015% -> tier 1 (0.50). ETH at 0.10% -> tier 3 (1.00).
    _seed_funding(rl, "BTC/USDT", [0.00015, 0.00015, 0.00015], mark_price=60_000.0)
    _seed_funding(rl, "ETH/USDT", [0.0010, 0.0010, 0.0010], mark_price=3_000.0)
    agent.run_once()
    btc = next(t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT")
    eth = next(t for t in tr.open_positions(agent="trading_funding") if t.ticker == "ETH/USDT")
    assert btc.signal_payload["size_tier_fraction"] == pytest.approx(0.5)
    assert eth.signal_payload["size_tier_fraction"] == pytest.approx(1.0)


def test_no_duplicate_entry_same_symbol(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    agent.run_once()
    btc = [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT"]
    assert len(btc) == 1


# --------------------------------------------------------------- exit tests


def test_exits_when_funding_below_exit_threshold(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    assert tr.open_positions(agent="trading_funding")

    fresh = datetime.now(timezone.utc) + timedelta(minutes=1)
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=0.00002, payload={"mark_price": 60_000.0},
        ts=fresh,
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


def test_exits_when_two_negative_prints_in_a_row(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    base = datetime.now(timezone.utc) + timedelta(seconds=1)
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=-0.00001, payload={"mark_price": 60_000.0},
        ts=base,
    ))
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=-0.00002, payload={"mark_price": 60_000.0},
        ts=base + timedelta(hours=8),
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


def test_holds_when_funding_above_exit_threshold(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    fresh = datetime.now(timezone.utc) + timedelta(minutes=1)
    # 0.008% per 8h: below enter (0.01%) but above exit (0.005%) -> still hold.
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=0.00008, payload={"mark_price": 60_000.0},
        ts=fresh,
    ))
    agent.run_once()
    btc = [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT"]
    assert len(btc) == 1


def test_exits_on_basis_blowout(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012], mark_price=60_000.0)
    agent.run_once()
    # 6% mark drift > funding_basis_close_pct (5%) -> close.
    fresh = datetime.now(timezone.utc) + timedelta(minutes=1)
    rl.write(WriteSignal(
        agent="research_crypto", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=0.00012, payload={"mark_price": 63_600.0},
        ts=fresh,
    ))
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


# --------------------------------------------------------------- cooldown


def test_cooldown_blocks_immediate_reentry(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    open_now = tr.open_positions(agent="trading_funding")[0]
    tr.close_trade(CloseTradeRequest(trade_id=open_now.id, exit_price=60_000.0))
    agent.run_once()
    assert not tr.open_positions(agent="trading_funding")


# --------------------------------------------------------------- robustness


def test_no_funding_history_is_safe(env) -> None:
    agent, _, tr = env
    agent.run_once()  # no positions, no crash
    assert not tr.open_positions(agent="trading_funding")


def test_per_symbol_isolation(env) -> None:
    agent, rl, tr = env
    _seed_funding(rl, "BTC/USDT", [0.00012, 0.00013, 0.00012])
    agent.run_once()
    btc = [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "BTC/USDT"]
    eth = [t for t in tr.open_positions(agent="trading_funding") if t.ticker == "ETH/USDT"]
    assert len(btc) == 1
    assert len(eth) == 0
