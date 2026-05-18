"""Weekly performance report — the paper-trading scorecard.

Tracks the 6 metrics that determine whether the system is ready to flip
PAPER_MODE=false:

  1. Sharpe ratio          target >= 0.8
  2. Max drawdown          target <= 12%
  3. Win rate              target >= 45%
  4. Avg win / avg loss    target >= 1.5x
  5. Per-agent pnl         target: each non-negative
  6. Signal-to-trade lag   target <= 5.0s

Each metric prints PASS/FAIL with the measured value. The overall verdict
is PASS only if every metric passes — that's a paper-trading readiness
signal, not a guarantee.

Outputs:
    reports/weekly.jsonl                  append-only per-week stats
    reports/weekly_YYYY-WW.txt            human-readable summary for ISO week
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from record.research_log import ResearchLog
from record.track_record import TrackRecord, Trade

# ----------------------------------------------------------------- thresholds


@dataclass(frozen=True)
class Thresholds:
    min_sharpe: float = 0.8
    max_drawdown: float = 0.12
    min_win_rate: float = 0.45
    min_payoff_ratio: float = 1.5
    max_signal_to_trade_seconds: float = 5.0


DEFAULT_THRESHOLDS = Thresholds()


# ----------------------------------------------------------------- result type


@dataclass
class MetricResult:
    name: str
    value: float | None
    target: str
    passed: bool
    detail: str = ""


@dataclass
class WeeklyReport:
    report_ts: str
    window_days: int
    n_trades: int
    metrics: list[MetricResult] = field(default_factory=list)
    per_agent_pnl: dict[str, float] = field(default_factory=dict)
    overall_pass: bool = False

    def to_dict(self) -> dict:
        return {
            "report_ts": self.report_ts,
            "window_days": self.window_days,
            "n_trades": self.n_trades,
            "overall_pass": self.overall_pass,
            "metrics": [
                {
                    "name": m.name,
                    "value": (round(m.value, 4) if m.value is not None else None),
                    "target": m.target,
                    "passed": m.passed,
                    "detail": m.detail,
                }
                for m in self.metrics
            ],
            "per_agent_pnl": {k: round(v, 2) for k, v in self.per_agent_pnl.items()},
        }


# ----------------------------------------------------------------- metric calcs


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _daily_returns(trades: list[Trade]) -> list[float]:
    by_day: dict[str, float] = {}
    for t in trades:
        if t.pnl is None or t.portfolio_value_at_entry <= 0 or t.exit_ts is None:
            continue
        day = _aware(t.exit_ts).date().isoformat()  # type: ignore[union-attr]
        by_day[day] = by_day.get(day, 0.0) + (t.pnl / t.portfolio_value_at_entry)
    return list(by_day.values())


def _sharpe(trades: list[Trade]) -> float | None:
    rets = _daily_returns(trades)
    if len(rets) < 2:
        return None
    mu = statistics.fmean(rets)
    sd = statistics.stdev(rets)
    if sd == 0:
        return None
    return (mu / sd) * math.sqrt(252)


def _max_drawdown(trades: list[Trade]) -> float:
    sorted_t = sorted(
        trades, key=lambda t: _aware(t.exit_ts) or datetime.min.replace(tzinfo=timezone.utc),
    )
    equity = peak = worst = 0.0
    for t in sorted_t:
        equity += t.pnl or 0.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak)
    return abs(worst)


def _win_rate(trades: list[Trade]) -> float | None:
    wins = sum(1 for t in trades if (t.pnl or 0.0) > 0)
    losses = sum(1 for t in trades if (t.pnl or 0.0) < 0)
    if wins + losses == 0:
        return None
    return wins / (wins + losses)


def _payoff_ratio(trades: list[Trade]) -> float | None:
    wins = [t.pnl for t in trades if (t.pnl or 0.0) > 0 and t.pnl is not None]
    losses = [abs(t.pnl) for t in trades if (t.pnl or 0.0) < 0 and t.pnl is not None]
    if not wins or not losses:
        return None
    avg_win = statistics.fmean(wins)
    avg_loss = statistics.fmean(losses)
    if avg_loss == 0:
        return None
    return avg_win / avg_loss


def _per_agent_pnl(trades: list[Trade]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        out[t.agent] = out.get(t.agent, 0.0) + (t.pnl or 0.0)
    return out


def _signal_to_trade_lag(trades: list[Trade], rl: ResearchLog) -> float | None:
    """Median seconds between the most recent research signal on this
    ticker and the trade entry. None if no measurable trades.
    """
    deltas: list[float] = []
    for t in trades:
        entry = _aware(t.entry_ts)
        if entry is None:
            continue
        latest = rl.latest(t.ticker, "sentiment_score")
        if latest is None:
            latest = rl.latest(t.ticker, "funding_rate")
        if latest is None:
            continue
        signal_ts = _aware(latest.ts)
        if signal_ts is None or signal_ts > entry:
            continue
        deltas.append((entry - signal_ts).total_seconds())
    if not deltas:
        return None
    return statistics.median(deltas)


# ----------------------------------------------------------------- build_report


def build_report(
    tr: TrackRecord,
    rl: ResearchLog,
    *,
    window_days: int = 7,
    now: datetime | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> WeeklyReport:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    trades = tr.closed_trades(since=since)

    metrics: list[MetricResult] = []

    sharpe = _sharpe(trades)
    metrics.append(MetricResult(
        name="sharpe_ratio",
        value=sharpe,
        target=f">= {thresholds.min_sharpe:.2f}",
        passed=sharpe is not None and sharpe >= thresholds.min_sharpe,
        detail="needs >= 2 distinct trading days" if sharpe is None else "",
    ))

    dd = _max_drawdown(trades) if trades else 0.0
    metrics.append(MetricResult(
        name="max_drawdown",
        value=dd,
        target=f"<= {thresholds.max_drawdown:.0%}",
        passed=dd <= thresholds.max_drawdown,
    ))

    wr = _win_rate(trades)
    metrics.append(MetricResult(
        name="win_rate",
        value=wr,
        target=f">= {thresholds.min_win_rate:.0%}",
        passed=wr is not None and wr >= thresholds.min_win_rate,
        detail="needs at least one closed trade with pnl != 0" if wr is None else "",
    ))

    payoff = _payoff_ratio(trades)
    metrics.append(MetricResult(
        name="payoff_ratio",
        value=payoff,
        target=f">= {thresholds.min_payoff_ratio:.2f}",
        passed=payoff is not None and payoff >= thresholds.min_payoff_ratio,
        detail="needs at least one win and one loss" if payoff is None else "",
    ))

    per_agent = _per_agent_pnl(trades)
    no_negative_agent = all(v >= 0 for v in per_agent.values()) if per_agent else True
    losers = [k for k, v in per_agent.items() if v < 0]
    metrics.append(MetricResult(
        name="per_agent_pnl",
        value=None,
        target="every agent pnl >= 0",
        passed=no_negative_agent,
        detail=("losing agents: " + ", ".join(losers)) if losers else "",
    ))

    lag = _signal_to_trade_lag(trades, rl)
    metrics.append(MetricResult(
        name="signal_to_trade_lag_s",
        value=lag,
        target=f"<= {thresholds.max_signal_to_trade_seconds:.1f}s",
        passed=lag is None or lag <= thresholds.max_signal_to_trade_seconds,
        detail="no measurable trades" if lag is None else "",
    ))

    overall = all(m.passed for m in metrics)
    return WeeklyReport(
        report_ts=now.isoformat(timespec="seconds"),
        window_days=window_days,
        n_trades=len(trades),
        metrics=metrics,
        per_agent_pnl=per_agent,
        overall_pass=overall,
    )


# ----------------------------------------------------------------- render + write


def render_text(report: WeeklyReport) -> str:
    out: list[str] = []
    verdict = "PASS" if report.overall_pass else "FAIL"
    out.append(f"AlphaGrid weekly report - {report.report_ts}")
    out.append(f"window: last {report.window_days}d    trades: {report.n_trades}    verdict: {verdict}")
    out.append("-" * 68)
    for m in report.metrics:
        mark = "[PASS]" if m.passed else "[FAIL]"
        v = f"{m.value:.4f}" if isinstance(m.value, float) else "n/a"
        out.append(f"  {mark} {m.name:<24} value={v:>10}  target={m.target}  {m.detail}")
    out.append("-" * 68)
    if report.per_agent_pnl:
        out.append("  per-agent pnl:")
        for agent, pnl in sorted(report.per_agent_pnl.items(), key=lambda kv: -kv[1]):
            out.append(f"    {agent:<28} {pnl:>10.2f}")
    return "\n".join(out) + "\n"


def write_report(report: WeeklyReport, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = reports_dir / "weekly.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report.to_dict(), default=str) + "\n")
    dt = datetime.fromisoformat(report.report_ts)
    iso_year, iso_week, _ = dt.isocalendar()
    txt_path = reports_dir / f"weekly_{iso_year}-W{iso_week:02d}.txt"
    txt_path.write_text(render_text(report), encoding="utf-8")
    return jsonl_path, txt_path


# ----------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    report = build_report(TrackRecord(), ResearchLog(), window_days=args.window_days)
    jsonl_path, txt_path = write_report(report, args.reports_dir)
    print(render_text(report))
    print(f"appended: {jsonl_path}")
    print(f"summary : {txt_path}")
    return 0 if report.overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
