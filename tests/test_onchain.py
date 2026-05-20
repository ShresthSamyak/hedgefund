"""On-chain feed tests + research_crypto integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.research_crypto import ResearchCrypto
from data.feeds_crypto import StaticCryptoFeed
from data.onchain import CoinMetricsClient, MvrvPoint, StaticOnChainFeed, _parse_iso
from record.research_log import ResearchLog

# ---------------------------------------------------------------- StaticOnChainFeed


def test_static_feed_returns_set_value() -> None:
    feed = StaticOnChainFeed()
    feed.set_mvrv("btc", 1.42)
    pt = feed.fetch_mvrv("btc")
    assert pt is not None
    assert pt.asset == "btc"
    assert pt.value == 1.42


def test_static_feed_returns_none_when_unset() -> None:
    feed = StaticOnChainFeed()
    assert feed.fetch_mvrv("btc") is None


def test_static_feed_set_none_explicitly() -> None:
    feed = StaticOnChainFeed()
    feed.set_mvrv("btc", None)
    assert feed.fetch_mvrv("btc") is None


# ---------------------------------------------------------------- CoinMetrics


def test_parse_iso_handles_nanosecond_precision() -> None:
    ts = _parse_iso("2026-05-19T00:00:00.000000000Z")
    assert ts.tzinfo is not None
    assert ts.year == 2026 and ts.month == 5 and ts.day == 19


def test_parse_iso_handles_microseconds() -> None:
    ts = _parse_iso("2026-05-19T12:34:56.789012Z")
    assert ts.hour == 12 and ts.minute == 34


def test_coinmetrics_client_parses_real_response_shape() -> None:
    """Mock urlopen with the actual JSON shape CoinMetrics returns."""
    body = (
        b'{"data": [{"asset": "btc", "time": "2026-05-19T00:00:00.000000000Z", '
        b'"CapMVRVCur": "1.234"}]}'
    )

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with patch("data.onchain.urlopen", return_value=_FakeResponse()):
        client = CoinMetricsClient()
        pt = client.fetch_mvrv("btc")
    assert pt is not None
    assert pt.asset == "btc"
    assert pt.value == 1.234


def test_coinmetrics_returns_none_on_http_error() -> None:
    class _FakeResponse:
        status = 500

        def read(self) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with patch("data.onchain.urlopen", return_value=_FakeResponse()):
        client = CoinMetricsClient()
        assert client.fetch_mvrv("btc") is None


def test_coinmetrics_returns_none_on_network_error() -> None:
    def _raise(*_a, **_k):
        raise OSError("network down")

    with patch("data.onchain.urlopen", side_effect=_raise):
        client = CoinMetricsClient()
        assert client.fetch_mvrv("btc") is None


def test_coinmetrics_returns_none_on_empty_data() -> None:
    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"data": []}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with patch("data.onchain.urlopen", return_value=_FakeResponse()):
        client = CoinMetricsClient()
        assert client.fetch_mvrv("btc") is None


# ---------------------------------------------------------------- research_crypto integration


def _seed_funding(feed: StaticCryptoFeed) -> None:
    feed.set("BTC/USDT", rate=0.0001, mark_price=60_000.0)
    feed.set("ETH/USDT", rate=0.0001, mark_price=3_000.0)


def test_research_crypto_writes_mvrv_when_onchain_present() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    feed = StaticCryptoFeed()
    _seed_funding(feed)
    onchain = StaticOnChainFeed()
    onchain.set_mvrv("btc", 1.85)

    agent = ResearchCrypto(feed=feed, research_log=rl, onchain=onchain)
    agent.run_once()

    rec = rl.latest("PORTFOLIO", "mvrv")
    assert rec is not None
    assert rec.value == 1.85
    assert rec.payload["source"] == "coinmetrics_community"


def test_research_crypto_skips_mvrv_when_onchain_none() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    feed = StaticCryptoFeed()
    _seed_funding(feed)

    agent = ResearchCrypto(feed=feed, research_log=rl, onchain=None)
    agent.run_once()

    # Regime + funding still written; no mvrv row.
    assert rl.latest("PORTFOLIO", "regime") is not None
    assert rl.latest("PORTFOLIO", "mvrv") is None


def test_research_crypto_swallows_onchain_errors() -> None:
    rl = ResearchLog(db_url="sqlite:///:memory:")
    feed = StaticCryptoFeed()
    _seed_funding(feed)

    class _Broken:
        def fetch_mvrv(self, asset: str = "btc"):
            raise RuntimeError("coinmetrics 500")

    agent = ResearchCrypto(feed=feed, research_log=rl, onchain=_Broken())
    # Should not raise — regime/funding writes still succeed.
    agent.run_once()
    assert rl.latest("PORTFOLIO", "regime") is not None
    assert rl.latest("PORTFOLIO", "mvrv") is None


def test_research_crypto_skips_when_onchain_returns_none() -> None:
    """Onchain feed configured but returns None (rate-limited / no data)."""
    rl = ResearchLog(db_url="sqlite:///:memory:")
    feed = StaticCryptoFeed()
    _seed_funding(feed)
    onchain = StaticOnChainFeed()   # nothing set

    agent = ResearchCrypto(feed=feed, research_log=rl, onchain=onchain)
    agent.run_once()
    assert rl.latest("PORTFOLIO", "mvrv") is None


def test_mvrv_propagates_into_regime_gate() -> None:
    """End-to-end: research_crypto writes mvrv → trading_crypto_sent reads it."""
    from agents.trading_crypto_sent import TradingCryptoSent

    rl = ResearchLog(db_url="sqlite:///:memory:")
    feed = StaticCryptoFeed()
    _seed_funding(feed)
    onchain = StaticOnChainFeed()
    onchain.set_mvrv("btc", 0.8)   # < 1.0 → bullish on-chain

    ResearchCrypto(feed=feed, research_log=rl, onchain=onchain).run_once()
    TradingCryptoSent(research_log=rl).run_once()

    modifier = rl.latest("PORTFOLIO", "crypto_size_modifier")
    assert modifier is not None
    # MVRV 0.8 (bullish, +1) averaged with regime score should still be > 0
    assert modifier.value > 0.0
    assert "mvrv" in modifier.payload["components"]
    assert modifier.payload["components"]["mvrv"] == 1.0
