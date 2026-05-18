"""End-to-end dashboard preview without touching live data.

Simulates N days of paper trading against synthetic (or real) historical
OHLC + funding, writes everything to an isolated SQLite DB, then prints
the exact commands you'd run to point the dashboard at it.

Use this once before the actual burn-in to confirm the full chain works:

    python -m tools.dry_run                    # 7d synthetic, no LLM
    python -m tools.dry_run --days 30          # longer window
    python -m tools.dry_run --live             # pull real Binance + yfinance
    python -m tools.dry_run --with-llm         # build llm_reason + narratives
    python -m tools.dry_run --db /tmp/dry.db   # explicit output path

After it finishes:

    # Terminal 1 — point the API at the dry-run DB
    set ALPHAGRID_DB_URL=sqlite:///./reports/dry_run.db
    uvicorn api.main:app --port 8000

    # Terminal 2 — start the dashboard
    cd web && npm run dev

The dashboard will show ~28 trades/day across 8 agents with the full
reasoning chain rendered: rule-based reason + LLM rationale + per-agent
P&L + cumulative equity curve.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
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
from models.finbert_scorer import NullScorer
from models.llm_client import LLMClient, NullLLM, build_llm_client
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from tools.backtest import _load_binance_history, _load_synthetic, _load_yfinance_history
from tools.weekly_report import build_report, render_text

log = logging.getLogger("alphagrid.dry_run")

DEFAULT_DB_PATH = Path("reports") / "dry_run.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7,
                        help="how many sim-days to replay (default 7)")
    parser.add_argument("--step-hours", type=int, default=4,
                        help="sim-clock step in hours (default 4)")
    parser.add_argument("--portfolio-value", type=float, default=10_000.0)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help="output sqlite path (deleted + recreated each run)")
    parser.add_argument("--live", action="store_true",
                        help="pull real Binance/yfinance data instead of synthetic")
    parser.add_argument("--with-llm", action="store_true",
                        help="build llm_reason + narratives during the dry-run")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(name)s %(message)s")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    # Fresh DB every run so dry-runs are reproducible.
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        args.db.unlink()
    db_url = f"sqlite:///{args.db.resolve()}"
    log.info("dry-run db: %s", db_url)

    # Universes (use settings defaults).
    from config.settings import get_settings
    settings = get_settings()
    crypto_symbols = sorted(set(
        list(settings.strategy.funding_universe) + list(settings.strategy.trend_universe)
    ))
    india_tickers = sorted(set(
        list(settings.strategy.momentum_universe)
        + list(settings.strategy.sentiment_universe)
        + [t for pair in settings.strategy.pairs_universe for t in pair]
    ))

    # Data loaders.
    if args.live:
        try:
            crypto_ohlc, crypto_funding = _load_binance_history(crypto_symbols, args.days)
            india_ohlc = _load_yfinance_history(india_tickers, args.days)
            print(f"[ok] pulled live data: {len(crypto_ohlc)} crypto / {len(india_ohlc)} india tickers")
        except Exception as exc:
            print(f"[warn] live data fetch failed ({exc}); falling back to synthetic")
            crypto_ohlc, crypto_funding, india_ohlc = _load_synthetic(start, end)
    else:
        crypto_ohlc, crypto_funding, india_ohlc = _load_synthetic(start, end)

    # Wiring.
    track_record = TrackRecord(db_url=db_url)
    research_log = ResearchLog(db_url=db_url)
    clock = VirtualClock(start)
    india_feed = HistoricalIndiaFeed(clock)
    crypto_feed = HistoricalCryptoFeed(clock)
    for sym, bars in crypto_ohlc.items():
        crypto_feed.load_ohlc(sym, bars)
    for sym, pts in crypto_funding.items():
        crypto_feed.load_funding_history(sym, pts)
    for tkr, bars in india_ohlc.items():
        india_feed.load_ohlc(tkr, bars)

    llm: LLMClient = build_llm_client() if args.with_llm else NullLLM()
    # Temporarily flip the enable toggle so the router actually calls the LLM
    # even if .env has it off.
    if args.with_llm:
        settings.vertex.enable_llm_summaries = True

    runner = BacktestRunner(
        clock=clock,
        track_record=track_record,
        research_log=research_log,
        llm=llm,
    )
    agents = [
        ResearchIndia(feed=india_feed, research_log=research_log, scorer=NullScorer(), llm=llm),
        ResearchCrypto(feed=crypto_feed, research_log=research_log),
        TradingFunding(
            research_log=research_log, track_record=track_record,
            trade_router=runner.router,
            portfolio_value_getter=lambda: args.portfolio_value,
        ),
        TradingMomentum(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=runner.router,
            portfolio_value_getter=lambda: args.portfolio_value, now_fn=clock.now,
        ),
        TradingSentiment(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=runner.router,
            portfolio_value_getter=lambda: args.portfolio_value, now_fn=clock.now,
        ),
        TradingPairs(
            feed=india_feed, research_log=research_log,
            track_record=track_record, trade_router=runner.router,
            portfolio_value_getter=lambda: args.portfolio_value, now_fn=clock.now,
        ),
        TradingTrend(
            feed=crypto_feed, research_log=research_log,
            track_record=track_record, trade_router=runner.router,
            portfolio_value_getter=lambda: args.portfolio_value,
        ),
        TradingCryptoSent(research_log=research_log),
    ]

    print(f"[run] simulating {args.days}d, step={args.step_hours}h, "
          f"start={start.isoformat()}, end={end.isoformat()}")
    result = runner.run(
        agents=agents, start=start, end=end,
        step=timedelta(hours=args.step_hours),
    )
    print(f"[done] ticks={result.n_ticks}  invocations={sum(result.agent_invocations.values())}  "
          f"open={len(track_record.open_positions())}  "
          f"closed={len(track_record.closed_trades())}")

    # Scorecard (same format as weekly_report).
    report = build_report(track_record, research_log,
                          window_days=args.days, now=end)
    print()
    print(render_text(report))
    print(f"runner summary: {json.dumps(result.summary(), default=str)}")

    # LLM rationales attached?
    closed = track_record.closed_trades(limit=200)
    with_llm = sum(1 for t in closed if (t.signal_payload or {}).get("llm_reason"))
    if args.with_llm:
        print(f"[llm] {with_llm}/{len(closed)} closed trades have llm_reason")
    else:
        print(f"[llm] disabled; {with_llm} trades carry llm_reason from prior runs")

    # Operator instructions.
    abs_db = args.db.resolve()
    print()
    print("=" * 68)
    print("Dry-run complete. To preview in the dashboard without touching live data:")
    print()
    print("  # Terminal 1 — point the API at this DB")
    print(f"  $env:ALPHAGRID_DB_URL = 'sqlite:///{abs_db}'   # PowerShell")
    print(f"  export ALPHAGRID_DB_URL='sqlite:///{abs_db}'  # bash/zsh")
    print("  uvicorn api.main:app --port 8000")
    print()
    print("  # Terminal 2 — start the dashboard")
    print("  cd web; npm run dev")
    print()
    print("  Then open http://localhost:3000")
    print("=" * 68)

    return 0 if report.overall_pass else 0   # exit 0 either way — informational


if __name__ == "__main__":
    sys.exit(main())
