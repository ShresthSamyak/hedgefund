"""Backtest CLI.

Pulls historical OHLC + funding data from the live providers, replays the
configured agents through the BacktestRunner, then prints the same metric
table the weekly_report tool produces — so live results and backtest
results are directly comparable for the "live matches backtest within 20%"
paper-to-live trigger.

Usage:
    python -m tools.backtest                          # last 60 days, all agents
    python -m tools.backtest --days 90 --agents trading_funding,trading_momentum
    python -m tools.backtest --offline                # synthetic data, no network
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.research_crypto import ResearchCrypto
from agents.research_india import ResearchIndia
from agents.trading_crypto_sent import TradingCryptoSent
from agents.trading_funding import TradingFunding
from agents.trading_momentum import TradingMomentum
from agents.trading_pairs import TradingPairs
from agents.trading_sentiment import TradingSentiment
from agents.trading_trend import TradingTrend
from backtest.clock import VirtualClock
from backtest.historical_feeds import HistoricalCryptoFeed, HistoricalIndiaFeed
from backtest.runner import BacktestRunner
from config.settings import get_settings
from data.feeds_crypto import DatedCryptoBar, FundingPoint
from data.feeds_india import DatedBar
from models.finbert_scorer import NullScorer
from models.indicators import OHLCBar
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from tools.weekly_report import build_report, render_text

log = logging.getLogger("alphagrid.backtest")


AGENT_REGISTRY = {
    "research_india":      "india",
    "research_crypto":     "crypto",
    "trading_funding":     "crypto",
    "trading_momentum":    "india",
    "trading_sentiment":   "india",
    "trading_pairs":       "india",
    "trading_trend":       "crypto",
    "trading_crypto_sent": "regime",
}


# ----------------------------------------------------------------- data loaders


def _load_binance_history(symbols: list[str], days: int) -> tuple[
    dict[str, list[DatedCryptoBar]], dict[str, list[FundingPoint]]
]:
    """Pull 4h OHLC + funding history via ccxt."""
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    ohlc: dict[str, list[DatedCryptoBar]] = {}
    funding: dict[str, list[FundingPoint]] = {}
    for sym in symbols:
        log.info("fetching %s OHLC (%dd) + funding history", sym, days)
        try:
            raws = ex.fetch_ohlcv(sym, timeframe="4h", since=since_ms, limit=1000)
        except Exception as exc:
            log.warning("ohlcv fetch failed for %s: %s — switching to spot", sym, exc)
            spot = ccxt.binance({"enableRateLimit": True})
            raws = spot.fetch_ohlcv(sym, timeframe="4h", since=since_ms, limit=1000)
        ohlc[sym] = [
            DatedCryptoBar(
                ts=datetime.fromtimestamp(int(r[0]) / 1000.0, tz=timezone.utc),
                bar=OHLCBar(open=float(r[1]), high=float(r[2]), low=float(r[3]),
                            close=float(r[4]), volume=float(r[5])),
            )
            for r in raws
        ]
        try:
            fh = ex.fetch_funding_rate_history(sym, since=since_ms, limit=1000)
        except Exception as exc:
            log.warning("funding history failed for %s: %s", sym, exc)
            fh = []
        funding[sym] = [
            FundingPoint(
                symbol=sym,
                rate=float(r.get("fundingRate") or 0.0),
                funding_time=datetime.fromtimestamp(int(r.get("timestamp") or 0) / 1000.0, tz=timezone.utc),
                mark_price=float(r["markPrice"]) if r.get("markPrice") is not None else None,
            )
            for r in fh
        ]
    return ohlc, funding


def _load_yfinance_history(tickers: list[str], days: int) -> dict[str, list[DatedBar]]:
    import yfinance as yf
    out: dict[str, list[DatedBar]] = {}
    for t in tickers:
        sym = t if t.endswith(".NS") else f"{t}.NS"
        log.info("fetching %s daily OHLC (%dd)", sym, days)
        hist = yf.Ticker(sym).history(period=f"{int(days * 1.5)}d", interval="1d", auto_adjust=False)
        if hist.empty:
            out[t] = []
            continue
        bars: list[DatedBar] = []
        for ts_idx, row in hist.iterrows():
            ts = ts_idx.to_pydatetime().astimezone(timezone.utc)
            bars.append(DatedBar(
                ts=ts,
                bar=OHLCBar(
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]),  close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                ),
            ))
        out[t] = bars
    return out


def _load_synthetic(start: datetime, end: datetime) -> tuple[
    dict[str, list[DatedCryptoBar]],
    dict[str, list[FundingPoint]],
    dict[str, list[DatedBar]],
]:
    """Deterministic series so --offline runs don't hit the network."""
    import math
    days = max(1, int((end - start).total_seconds() / 86400) + 1)
    # 4h crypto bars w/ a slow drift + noise
    crypto: dict[str, list[DatedCryptoBar]] = {}
    funding: dict[str, list[FundingPoint]] = {}
    for sym, base_price in (("BTC/USDT", 60_000.0), ("ETH/USDT", 3_000.0)):
        bars = []
        for i in range(days * 6):
            ts = start + timedelta(hours=4 * i)
            drift = 1.0 + 0.0001 * i
            noise = 1.0 + 0.005 * math.sin(i / 7)
            c = base_price * drift * noise
            bars.append(DatedCryptoBar(
                ts=ts,
                bar=OHLCBar(open=c, high=c * 1.002, low=c * 0.998, close=c, volume=1.0),
            ))
        crypto[sym] = bars
        funding[sym] = [
            FundingPoint(
                symbol=sym,
                rate=0.00015 + 5e-5 * math.sin(i / 5),
                funding_time=start + timedelta(hours=8 * i),
                mark_price=base_price,
            )
            for i in range(days * 3)
        ]
    india_tickers = ("HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS",
                     "BAJFINANCE", "LT", "KOTAKBANK")
    india: dict[str, list[DatedBar]] = {}
    for j, t in enumerate(india_tickers):
        base_price = 1000 + j * 250
        bars = []
        for i in range(days + 30):
            ts = start + timedelta(days=i)
            drift = 1.0 + 0.0005 * i
            noise = 1.0 + 0.01 * math.sin(i / 13 + j)
            c = base_price * drift * noise
            bars.append(DatedBar(
                ts=ts,
                bar=OHLCBar(open=c, high=c * 1.005, low=c * 0.995, close=c, volume=2_000_000),
            ))
        india[t] = bars
    return crypto, funding, india


# ----------------------------------------------------------------- agents


def _build_agents(
    selected: list[str],
    *,
    india_feed: HistoricalIndiaFeed,
    crypto_feed: HistoricalCryptoFeed,
    research_log: ResearchLog,
    track_record: TrackRecord,
    router,
    portfolio_value: float,
    now_fn,
) -> list:
    out = []
    if "research_india" in selected:
        out.append(ResearchIndia(feed=india_feed, research_log=research_log, scorer=NullScorer()))
    if "research_crypto" in selected:
        out.append(ResearchCrypto(feed=crypto_feed, research_log=research_log))
    if "trading_funding" in selected:
        out.append(TradingFunding(
            research_log=research_log, track_record=track_record,
            trade_router=router, portfolio_value_getter=lambda: portfolio_value,
        ))
    if "trading_momentum" in selected:
        out.append(TradingMomentum(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=router,
            portfolio_value_getter=lambda: portfolio_value, now_fn=now_fn,
        ))
    if "trading_sentiment" in selected:
        out.append(TradingSentiment(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=router,
            portfolio_value_getter=lambda: portfolio_value, now_fn=now_fn,
        ))
    if "trading_pairs" in selected:
        out.append(TradingPairs(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=router,
            portfolio_value_getter=lambda: portfolio_value, now_fn=now_fn,
        ))
    if "trading_trend" in selected:
        out.append(TradingTrend(
            feed=crypto_feed, research_log=research_log,
            track_record=track_record, trade_router=router,
            portfolio_value_getter=lambda: portfolio_value,
        ))
    if "trading_crypto_sent" in selected:
        out.append(TradingCryptoSent(research_log=research_log))
    return out


# ----------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--step-hours", type=int, default=4)
    parser.add_argument("--portfolio-value", type=float, default=10_000.0)
    parser.add_argument(
        "--agents",
        default=",".join(AGENT_REGISTRY.keys()),
        help="comma-separated agent names to include",
    )
    parser.add_argument("--offline", action="store_true",
                        help="use synthetic data instead of pulling from Binance/yfinance")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    selected = [a.strip() for a in args.agents.split(",") if a.strip()]
    unknown = [a for a in selected if a not in AGENT_REGISTRY]
    if unknown:
        parser.error(f"unknown agents: {unknown}; known: {list(AGENT_REGISTRY)}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    settings = get_settings()
    crypto_symbols = list(settings.strategy.funding_universe) + list(settings.strategy.trend_universe)
    crypto_symbols = sorted(set(crypto_symbols))
    india_tickers = sorted(set(
        list(settings.strategy.momentum_universe)
        + list(settings.strategy.sentiment_universe)
        + [t for pair in settings.strategy.pairs_universe for t in pair]
    ))

    if args.offline:
        crypto_ohlc, crypto_funding, india_ohlc = _load_synthetic(start, end)
    else:
        crypto_ohlc, crypto_funding = _load_binance_history(crypto_symbols, args.days)
        india_ohlc = _load_yfinance_history(india_tickers, args.days)

    # Backtest needs an isolated DB so we don't pollute live data.
    tmp = Path(tempfile.mkdtemp(prefix="alphagrid-bt-"))
    db_url = f"sqlite:///{tmp / 'backtest.db'}"
    track_record = TrackRecord(db_url=db_url)
    research_log = ResearchLog(db_url=db_url)
    log.info("backtest db: %s", db_url)

    clock = VirtualClock(start)
    india_feed = HistoricalIndiaFeed(clock)
    crypto_feed = HistoricalCryptoFeed(clock)
    for sym, bars in crypto_ohlc.items():
        crypto_feed.load_ohlc(sym, bars)
    for sym, pts in crypto_funding.items():
        crypto_feed.load_funding_history(sym, pts)
    for tkr, bars in india_ohlc.items():
        india_feed.load_ohlc(tkr, bars)

    runner = BacktestRunner(clock=clock, track_record=track_record, research_log=research_log)
    agents = _build_agents(
        selected,
        india_feed=india_feed, crypto_feed=crypto_feed,
        research_log=research_log, track_record=track_record,
        router=runner.router, portfolio_value=args.portfolio_value,
        now_fn=clock.now,
    )

    result = runner.run(
        agents=agents,
        start=start, end=end,
        step=timedelta(hours=args.step_hours),
    )

    # Reuse the weekly_report scorecard so live and backtest output is comparable.
    report = build_report(track_record, research_log,
                          window_days=args.days, now=end)
    rendered = render_text(report)
    print(rendered)
    print(f"runner summary: {json.dumps(result.summary(), default=str)}")

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    tag = f"backtest_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    txt_path = args.reports_dir / f"{tag}.txt"
    json_path = args.reports_dir / f"{tag}.json"
    txt_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps({
        "summary": result.summary(),
        "report": report.to_dict(),
    }, default=str, indent=2), encoding="utf-8")
    print(f"summary saved to: {txt_path}")
    print(f"json saved to:    {json_path}")

    return 0 if report.overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
