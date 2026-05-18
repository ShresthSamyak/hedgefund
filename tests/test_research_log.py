from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from record.research_log import ResearchLog, ResearchLogError, WriteSignal


@pytest.fixture
def rl() -> ResearchLog:
    return ResearchLog(db_url="sqlite:///:memory:")


def _sig(**overrides) -> WriteSignal:
    base = dict(
        agent="research_crypto",
        market="crypto",
        ticker="BTC/USDT",
        signal_type="funding_rate",
        value=0.012,
        payload={"window": "8h"},
    )
    base.update(overrides)
    return WriteSignal(**base)  # type: ignore[arg-type]


def test_write_and_latest(rl: ResearchLog) -> None:
    rl.write(_sig(value=0.010))
    rl.write(_sig(value=0.014))
    latest = rl.latest("BTC/USDT", "funding_rate")
    assert latest is not None and latest.value == 0.014


def test_recent_window_filters(rl: ResearchLog) -> None:
    old = _now() - timedelta(hours=10)
    new = _now() - timedelta(minutes=5)
    rl.write(_sig(value=0.010, ts=old))
    rl.write(_sig(value=0.014, ts=new))
    recent = rl.recent("funding_rate", window=timedelta(hours=1))
    assert len(recent) == 1
    assert recent[0].value == 0.014


def test_recent_orders_descending(rl: ResearchLog) -> None:
    t0 = _now() - timedelta(minutes=30)
    t1 = _now() - timedelta(minutes=20)
    t2 = _now() - timedelta(minutes=10)
    rl.write(_sig(value=1.0, ts=t0))
    rl.write(_sig(value=2.0, ts=t1))
    rl.write(_sig(value=3.0, ts=t2))
    rows = rl.recent("funding_rate", window=timedelta(hours=1))
    assert [r.value for r in rows] == [3.0, 2.0, 1.0]


def test_per_ticker_filter(rl: ResearchLog) -> None:
    rl.write(_sig(ticker="BTC/USDT", value=0.012))
    rl.write(_sig(ticker="ETH/USDT", value=0.020))
    btc = rl.recent("funding_rate", window=timedelta(hours=1), ticker="BTC/USDT")
    assert len(btc) == 1 and btc[0].ticker == "BTC/USDT"


def test_invalid_signal_raises(rl: ResearchLog) -> None:
    with pytest.raises(ResearchLogError):
        rl.write(_sig(ticker=""))


def test_non_json_payload_raises(rl: ResearchLog) -> None:
    class Weird:
        pass

    with pytest.raises(ResearchLogError):
        rl.write(_sig(payload={"obj": Weird()}))


def test_batch_writes_all(rl: ResearchLog) -> None:
    ids = rl.write_batch([
        _sig(ticker="BTC/USDT", value=0.01),
        _sig(ticker="ETH/USDT", value=0.02),
        _sig(ticker="SOL/USDT", value=0.03),
    ])
    assert len(ids) == 3
    assert len(rl.recent("funding_rate", window=timedelta(hours=1))) == 3


def _now() -> datetime:
    return datetime.now(timezone.utc)
