"""Pre-burn-in pipeline health check.

Run before committing to a multi-day continuous run:

    python -m tools.healthcheck            # full check (~30s, network)
    python -m tools.healthcheck --offline  # skip network-dependent checks

Each check is annotated essential (E) or optional (O). The script returns
exit 0 iff every essential check passes; optional failures only warn.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("alphagrid.healthcheck")

Status = str  # "PASS" | "FAIL" | "SKIP"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    essential: bool = True
    duration_ms: int = 0


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    def essential_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.essential and r.status == "FAIL"]

    def optional_failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.essential and r.status == "FAIL"]

    def exit_code(self) -> int:
        return 1 if self.essential_failures() else 0

    def render(self) -> str:
        lines = ["", "AlphaGrid health check", "=" * 60]
        for r in self.results:
            badge = "E" if r.essential else "O"
            mark = {"PASS": "[OK]", "FAIL": "[FAIL]", "SKIP": "[skip]"}.get(r.status, "[?]")
            lines.append(f"  {mark} [{badge}] {r.name:<32} ({r.duration_ms:>5}ms)  {r.detail}")
        lines.append("=" * 60)
        n_pass = sum(1 for r in self.results if r.status == "PASS")
        n_fail = sum(1 for r in self.results if r.status == "FAIL")
        n_skip = sum(1 for r in self.results if r.status == "SKIP")
        lines.append(f"  total: {len(self.results)}  pass: {n_pass}  fail: {n_fail}  skip: {n_skip}")
        if self.essential_failures():
            lines.append("")
            lines.append("ESSENTIAL FAILURES — do not start the burn-in until fixed.")
        elif n_fail:
            lines.append("")
            lines.append("Optional failures present; burn-in can proceed.")
        else:
            lines.append("")
            lines.append("All systems green. Ready for burn-in.")
        return "\n".join(lines)


CheckFn = Callable[[], tuple[Status, str]]


def run_check(name: str, fn: CheckFn, *, essential: bool = True) -> CheckResult:
    start = time.monotonic()
    try:
        status, detail = fn()
    except Exception as exc:
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        log.debug("check %s raised", name, exc_info=True)
    return CheckResult(
        name=name,
        status=status,
        detail=detail,
        essential=essential,
        duration_ms=int((time.monotonic() - start) * 1000),
    )


# -------------------------------------------------------------- check functions

def check_settings() -> tuple[Status, str]:
    from config.settings import get_settings
    s = get_settings()
    return "PASS", f"paper={s.runtime.paper_mode} db={s.runtime.alphagrid_db_url}"


def check_database_roundtrip() -> tuple[Status, str]:
    """Create both logs against a temp DB, write and read one row each."""
    from record.research_log import ResearchLog, WriteSignal
    from record.track_record import OpenTradeRequest, TrackRecord
    tmp = Path(tempfile.mkdtemp()) / "hc.db"
    url = f"sqlite:///{tmp}"
    tr = TrackRecord(db_url=url)
    rl = ResearchLog(db_url=url)
    tid = tr.open_trade(OpenTradeRequest(
        agent="healthcheck", market="crypto", ticker="BTC/USDT", side="BUY",
        qty=0.001, entry_price=60_000.0, portfolio_value_at_entry=10_000.0,
        reason_text="health", signal_payload={},
    ))
    rl.write(WriteSignal(
        agent="healthcheck", market="crypto", ticker="BTC/USDT",
        signal_type="funding_rate", value=0.0001, payload={"hc": True},
    ))
    assert tr.get(tid).ticker == "BTC/USDT"
    assert rl.latest("BTC/USDT", "funding_rate") is not None
    return "PASS", f"sqlite roundtrip ok ({tmp})"


def check_finbert(offline: bool) -> tuple[Status, str]:
    if offline:
        return "SKIP", "offline mode"
    from models.finbert_scorer import FinBertScorer
    s = FinBertScorer()
    score = s.score("HDFC Bank beats earnings expectations")
    if score.signed <= 0:
        return "FAIL", f"expected positive on bullish headline, got {score.signed:+.2f}"
    return "PASS", f"signed={score.signed:+.2f} label={score.label}"


def check_google_news_rss(offline: bool) -> tuple[Status, str]:
    if offline:
        return "SKIP", "offline mode"
    url = "https://news.google.com/rss/search?q=RELIANCE&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            ok = resp.status == 200
            body_size = len(resp.read(2048))
    except Exception as exc:
        return "FAIL", f"{type(exc).__name__}: {exc}"
    if not ok or body_size < 200:
        return "FAIL", f"status={ok} body_size={body_size}"
    return "PASS", f"google news rss ok ({body_size}+ bytes)"


def check_risk_manager() -> tuple[Status, str]:
    """Reject an obviously bad proposal, approve a clean one."""
    from datetime import timezone as tz
    from zoneinfo import ZoneInfo

    from record.track_record import TrackRecord
    from risk.risk_manager import FixedClock, RiskManager, TradeProposal
    tr = TrackRecord(db_url="sqlite:///:memory:")
    ist = datetime(2026, 5, 18, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz.utc)
    rm = RiskManager(tr, clock=FixedClock(ist))
    bad = TradeProposal(
        agent="hc", market="india", ticker="HDFCBANK", side="BUY",
        horizon="intraday", intended_qty=1.0, reference_price=1500.0,
        portfolio_value=0.0,                              # invalid
        signal_payload={}, reason_text="hc",
    )
    d_bad = rm.review(bad)
    if d_bad.approved:
        return "FAIL", "invalid proposal was approved"
    good = TradeProposal(
        agent="hc", market="india", ticker="HDFCBANK", side="BUY",
        horizon="intraday", intended_qty=1.0, reference_price=1500.0,
        portfolio_value=10_000.0, signal_payload={}, reason_text="hc",
    )
    d_good = rm.review(good)
    if not d_good.approved:
        return "FAIL", f"clean proposal rejected: {d_good.reason}"
    return "PASS", f"sized_qty={d_good.sized_qty:.4f}"


def check_trade_router_paper() -> tuple[Status, str]:
    """End-to-end paper trade: proposal -> risk -> approval -> log."""
    from datetime import timezone as tz
    from zoneinfo import ZoneInfo

    from comms.approval_gate import NullApprovalGate
    from execution.trade_router import TradeRouter
    from record.track_record import TrackRecord
    from risk.risk_manager import FixedClock, RiskManager, TradeProposal

    tr = TrackRecord(db_url="sqlite:///:memory:")
    ist = datetime(2026, 5, 18, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz.utc)
    rm = RiskManager(tr, clock=FixedClock(ist))
    router = TradeRouter(
        risk_manager=rm, approval_gate=NullApprovalGate(),
        track_record=tr, require_human_approval=False,
    )
    proposal = TradeProposal(
        agent="hc", market="india", ticker="HDFCBANK", side="BUY",
        horizon="intraday", intended_qty=1.0, reference_price=1500.0,
        portfolio_value=10_000.0, signal_payload={"hc": True}, reason_text="hc",
    )
    outcome = router.submit(proposal)
    if outcome.state != "executed":
        return "FAIL", f"outcome={outcome.state} reason={outcome.reason}"
    return "PASS", f"trade_id={outcome.trade_id[:8] if outcome.trade_id else '?'}"


def check_binance_ws(offline: bool, *, timeout_seconds: int = 18) -> tuple[Status, str]:
    """Open a real WebSocket, wait for at least one closed 5s bar."""
    if offline:
        return "SKIP", "offline mode"
    from data.live_crypto_stream import BinanceWebSocketStream
    from infra.signal_bus import InMemoryBus

    bus = InMemoryBus()
    bars: list = []
    bus.subscribe("price.BTC/USDT", lambda _c, p: bars.append(p))

    async def _run() -> None:
        stream = BinanceWebSocketStream(symbols=["BTC/USDT"], bus=bus, timeframe_seconds=5)
        await stream.start()
        try:
            for _ in range(timeout_seconds):
                if bars:
                    return
                await asyncio.sleep(1)
        finally:
            await stream.stop()

    try:
        asyncio.run(_run())
    finally:
        bus.shutdown()

    if not bars:
        return "FAIL", f"no bar in {timeout_seconds}s — check connectivity"
    bar = bars[0]["bar"]
    return "PASS", f"BTC/USDT O={bar['open']:.2f} C={bar['close']:.2f}"


def check_news_poller_e2e(offline: bool) -> tuple[Status, str]:
    """Run the poller for one tick against the real Google News feed."""
    if offline:
        return "SKIP", "offline mode"
    from agents.news_poller import NewsPoller
    from data.feeds_india import GoogleNewsAndYFinanceFeed
    from infra.signal_bus import InMemoryBus
    from models.finbert_scorer import NullScorer
    from record.research_log import ResearchLog

    bus = InMemoryBus()
    rl = ResearchLog(db_url="sqlite:///:memory:")
    raw_count = [0]
    bus.subscribe("news.raw", lambda _c, _p: raw_count.__setitem__(0, raw_count[0] + 1))
    try:
        poller = NewsPoller(
            feed=GoogleNewsAndYFinanceFeed(), bus=bus, research_log=rl,
            scorer=NullScorer(),
            tickers=("RELIANCE",),
            alert_threshold=0.7, per_ticker_limit=3,
        )
        poller.run_once()
        bus.drain()
    finally:
        bus.shutdown()
    if raw_count[0] == 0:
        return "FAIL", "no news articles fetched"
    return "PASS", f"published {raw_count[0]} news.raw events"


# ----------------------------------------------------------------- orchestration


def run_all(offline: bool = False) -> HealthReport:
    report = HealthReport()
    report.add(run_check("settings load",        check_settings))
    report.add(run_check("sqlite roundtrip",     check_database_roundtrip))
    report.add(run_check("risk manager rules",   check_risk_manager))
    report.add(run_check("trade router paper",   check_trade_router_paper))
    report.add(run_check("finbert score",        lambda: check_finbert(offline), essential=False))
    report.add(run_check("google news rss",      lambda: check_google_news_rss(offline), essential=False))
    report.add(run_check("news poller e2e",      lambda: check_news_poller_e2e(offline), essential=False))
    report.add(run_check("binance ws + bar",     lambda: check_binance_ws(offline), essential=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip network-dependent checks")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    report = run_all(offline=args.offline)
    print(report.render())
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
