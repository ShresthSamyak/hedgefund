"""GET /performance/summary — one big call returning every number the
performance page needs.

Goal: zero hardcoded data in the frontend. Every metric on the page comes
from this endpoint, computed against the real TrackRecord + ResearchLog.

Shape:
  {
    portfolio:        { total_pnl, annualised_return, sharpe_30d, ..., days_running, paper_mode }
    agents:           [ { name, status, total_pnl, win_rate, ..., sparkline, best_trade, worst_trade } ]
    equity_curve:     [ { ts, total, <agent_name>: cumulative_pnl, ... } ]
    distribution:     { wins, losses, pnl_buckets, hold_buckets }
    correlation:      { agents: [..], matrix: [[..]] }   // None if insufficient data
    trades:           [ { id, ts, agent, ticker, side, qty, entry, exit, pnl, hold_seconds, reason, llm_reason } ]
  }
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from config.settings import get_settings
from record.track_record import Trade


router = APIRouter()


# ---------------------------------------------------------------- helpers


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _day_key(ts: datetime) -> str:
    return _aware(ts).date().isoformat()  # type: ignore[union-attr]


def _daily_returns(trades: list[Trade]) -> dict[str, float]:
    """Map day -> sum of (pnl / portfolio_value_at_entry) on that day."""
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.pnl is None or t.exit_ts is None or t.portfolio_value_at_entry <= 0:
            continue
        out[_day_key(t.exit_ts)] += t.pnl / t.portfolio_value_at_entry
    return dict(out)


def _sharpe(daily_rets: dict[str, float]) -> float | None:
    rets = list(daily_rets.values())
    if len(rets) < 2:
        return None
    mu = statistics.fmean(rets)
    sd = statistics.stdev(rets)
    if sd == 0:
        return None
    return (mu / sd) * math.sqrt(252)


def _max_drawdown(trades: list[Trade]) -> float:
    sorted_t = sorted(
        trades,
        key=lambda t: _aware(t.exit_ts) or datetime.min.replace(tzinfo=timezone.utc),
    )
    equity = peak = worst = 0.0
    for t in sorted_t:
        equity += t.pnl or 0.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak)
    return abs(worst)


def _hold_seconds(t: Trade) -> float | None:
    a, b = _aware(t.entry_ts), _aware(t.exit_ts)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    dx = math.sqrt(sum((v - mx) ** 2 for v in xs))
    dy = math.sqrt(sum((v - my) ** 2 for v in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# ---------------------------------------------------------------- per-agent stats


@dataclass
class AgentStats:
    name: str
    status: str
    total_pnl: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    sharpe: float | None
    max_drawdown: float
    avg_hold_seconds: float | None
    sparkline: list[float]
    best_trade: dict | None
    worst_trade: dict | None
    last_signal_ts: str | None
    open_positions: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "total_pnl": round(self.total_pnl, 2),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "sharpe": round(self.sharpe, 3) if self.sharpe is not None else None,
            "max_drawdown": round(self.max_drawdown, 4),
            "avg_hold_seconds": (
                round(self.avg_hold_seconds, 1) if self.avg_hold_seconds is not None else None
            ),
            "sparkline": [round(v, 2) for v in self.sparkline],
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "last_signal_ts": self.last_signal_ts,
            "open_positions": self.open_positions,
        }


def _summarise_trade_for_payload(t: Trade) -> dict:
    payload = t.signal_payload or {}
    entry = _aware(t.entry_ts)
    exit_ = _aware(t.exit_ts)
    return {
        "id": t.id,
        "ts": (exit_ or entry or datetime.now(timezone.utc)).isoformat(),
        "agent": t.agent,
        "ticker": t.ticker,
        "side": t.side,
        "qty": t.qty,
        "entry": t.entry_price,
        "exit": t.exit_price,
        "pnl": t.pnl,
        "hold_seconds": _hold_seconds(t),
        "reason": t.reason_text,
        "llm_reason": payload.get("llm_reason"),
        "open": t.exit_ts is None,
    }


def _agent_stats(
    name: str,
    closed: list[Trade],
    opens: list[Trade],
    last_signal_ts: str | None,
) -> AgentStats:
    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) < 0]
    total_pnl = sum((t.pnl or 0.0) for t in closed)
    win_rate = (len(wins) / (len(wins) + len(losses))) if (wins or losses) else 0.0
    sharpe = _sharpe(_daily_returns(closed))
    dd = _max_drawdown(closed)
    holds = [h for h in (_hold_seconds(t) for t in closed) if h is not None]
    avg_hold = statistics.fmean(holds) if holds else None

    # Cumulative sparkline (oldest -> newest closed).
    spark: list[float] = []
    running = 0.0
    for t in sorted(
        closed, key=lambda t: _aware(t.exit_ts) or datetime.min.replace(tzinfo=timezone.utc)
    ):
        running += t.pnl or 0.0
        spark.append(running)

    best = max(closed, key=lambda t: t.pnl or 0.0, default=None)
    worst = min(closed, key=lambda t: t.pnl or 0.0, default=None)
    best_d = (
        {"ticker": best.ticker, "pnl": round(best.pnl or 0.0, 2)}
        if best and best.pnl is not None and best.pnl > 0
        else None
    )
    worst_d = (
        {"ticker": worst.ticker, "pnl": round(worst.pnl or 0.0, 2)}
        if worst and worst.pnl is not None and worst.pnl < 0
        else None
    )

    status = "running"
    if total_pnl < 0:
        status = "losing"
    if not closed and not opens:
        status = "no_signal"

    return AgentStats(
        name=name,
        status=status,
        total_pnl=total_pnl,
        trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        sharpe=sharpe,
        max_drawdown=dd,
        avg_hold_seconds=avg_hold,
        sparkline=spark,
        best_trade=best_d,
        worst_trade=worst_d,
        last_signal_ts=last_signal_ts,
        open_positions=len(opens),
    )


# ---------------------------------------------------------------- equity curve


def _equity_curve(closed: list[Trade]) -> list[dict[str, Any]]:
    """Multi-agent cumulative-P&L points. Every entry has every agent's
    cumulative-to-date value so the frontend can render a multi-line chart
    with no further work.
    """
    if not closed:
        return []
    sorted_trades = sorted(
        closed, key=lambda t: _aware(t.exit_ts) or datetime.min.replace(tzinfo=timezone.utc)
    )
    agents = sorted({t.agent for t in sorted_trades})
    running: dict[str, float] = {a: 0.0 for a in agents}
    out: list[dict[str, Any]] = []
    for t in sorted_trades:
        running[t.agent] += t.pnl or 0.0
        ts = _aware(t.exit_ts) or datetime.now(timezone.utc)
        point: dict[str, Any] = {
            "ts": ts.isoformat(),
            "total": round(sum(running.values()), 2),
        }
        for a in agents:
            point[a] = round(running[a], 2)
        out.append(point)
    return out


# ---------------------------------------------------------------- distribution


def _pnl_histogram(closed: list[Trade], bins: int = 12) -> list[dict]:
    pnls = [t.pnl for t in closed if t.pnl is not None]
    if not pnls:
        return []
    lo, hi = min(pnls), max(pnls)
    if lo == hi:
        return [{"bucket": f"{lo:.2f}", "count": len(pnls)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in pnls:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    return [
        {
            "bucket": f"{lo + i * width:.2f}",
            "count": counts[i],
        }
        for i in range(bins)
    ]


def _hold_buckets(closed: list[Trade]) -> list[dict]:
    holds = [(t.agent, _hold_seconds(t), (t.pnl or 0.0) > 0) for t in closed]
    holds = [(a, h, w) for a, h, w in holds if h is not None]
    if not holds:
        return []
    bands = [
        ("<1h", 0, 3600),
        ("1-4h", 3600, 4 * 3600),
        ("4-24h", 4 * 3600, 24 * 3600),
        ("1-3d", 24 * 3600, 3 * 24 * 3600),
        (">3d", 3 * 24 * 3600, float("inf")),
    ]
    out = []
    for label, lo, hi in bands:
        in_band = [(a, w) for a, h, w in holds if lo <= h < hi]
        wins = sum(1 for _, w in in_band if w)
        out.append({
            "bucket": label,
            "trades": len(in_band),
            "wins": wins,
            "losses": len(in_band) - wins,
        })
    return out


def _win_loss_per_agent(closed: list[Trade]) -> list[dict]:
    by_agent: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in closed:
        if t.pnl is None:
            continue
        bucket = by_agent[t.agent]
        if t.pnl > 0:
            bucket["wins"] += 1
        elif t.pnl < 0:
            bucket["losses"] += 1
    return [
        {"agent": a, "wins": v["wins"], "losses": v["losses"]}
        for a, v in sorted(by_agent.items())
    ]


# ---------------------------------------------------------------- correlation


def _correlation_matrix(closed: list[Trade]) -> dict | None:
    by_agent: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    all_days: set[str] = set()
    for t in closed:
        if t.pnl is None or t.exit_ts is None:
            continue
        day = _day_key(t.exit_ts)
        by_agent[t.agent][day] += t.pnl
        all_days.add(day)
    agents = sorted(by_agent.keys())
    if len(agents) < 2 or len(all_days) < 3:
        return None
    days_sorted = sorted(all_days)
    series = {a: [by_agent[a].get(d, 0.0) for d in days_sorted] for a in agents}
    n = len(agents)
    matrix: list[list[float | None]] = [[1.0 if i == j else None for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            r = _correlation(series[agents[i]], series[agents[j]])
            matrix[i][j] = matrix[j][i] = r if r is not None else None
    return {"agents": agents, "matrix": matrix, "n_days": len(days_sorted)}


# ---------------------------------------------------------------- main route


@router.get("/performance/summary")
def performance_summary(window_days: int = Query(90, ge=1, le=3650)) -> dict[str, Any]:
    from api.main import STATE   # late import to avoid circular

    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)

    closed_all = STATE.track_record.closed_trades(since=since, limit=10_000)
    opens_all = STATE.track_record.open_positions()

    # ---------- portfolio
    total_pnl = sum((t.pnl or 0.0) for t in closed_all)
    daily_all = _daily_returns(closed_all)
    sharpe_30 = _sharpe({k: v for k, v in daily_all.items() if k >= (now - timedelta(days=30)).date().isoformat()})
    sharpe_90 = _sharpe({k: v for k, v in daily_all.items() if k >= (now - timedelta(days=90)).date().isoformat()})
    sharpe_all = _sharpe(daily_all)
    dd_all = _max_drawdown(closed_all)
    wins_total = sum(1 for t in closed_all if (t.pnl or 0) > 0)
    losses_total = sum(1 for t in closed_all if (t.pnl or 0) < 0)
    wr = wins_total / (wins_total + losses_total) if (wins_total + losses_total) else 0.0

    first_trade = min(
        (t for t in closed_all if t.entry_ts is not None),
        key=lambda t: _aware(t.entry_ts) or now,
        default=None,
    )
    if first_trade is not None and first_trade.entry_ts is not None:
        days_running = max(1, (now - (_aware(first_trade.entry_ts) or now)).days)
    else:
        days_running = 0

    annualised = None
    if days_running > 0 and closed_all:
        first_pv = first_trade.portfolio_value_at_entry if first_trade is not None else 0
        if first_pv > 0:
            ret = total_pnl / first_pv
            annualised = ret * (365.0 / days_running)

    portfolio = {
        "total_pnl": round(total_pnl, 2),
        "annualised_return": round(annualised, 4) if annualised is not None else None,
        "sharpe_30d": round(sharpe_30, 3) if sharpe_30 is not None else None,
        "sharpe_90d": round(sharpe_90, 3) if sharpe_90 is not None else None,
        "sharpe_all": round(sharpe_all, 3) if sharpe_all is not None else None,
        "max_drawdown": round(dd_all, 4),
        "win_rate": round(wr, 4),
        "total_trades": len(closed_all),
        "open_positions": len(opens_all),
        "days_running": days_running,
        "paper_mode": settings.runtime.paper_mode,
        "kill_switch_active": dd_all >= settings.risk.kill_switch_drawdown,
        "kill_switch_limit": settings.risk.kill_switch_drawdown,
        "window_days": window_days,
    }

    # ---------- per-agent
    # Include EVERY agent name we know about, even if it never traded, so
    # the grid shows empty states rather than hiding agents.
    known_agents = (
        "research_india", "research_crypto",
        "trading_funding", "trading_momentum", "trading_sentiment",
        "trading_pairs", "trading_trend", "trading_crypto_sent",
    )
    closed_by_agent: dict[str, list[Trade]] = defaultdict(list)
    opens_by_agent: dict[str, list[Trade]] = defaultdict(list)
    for t in closed_all:
        closed_by_agent[t.agent].append(t)
    for t in opens_all:
        opens_by_agent[t.agent].append(t)

    seen = set()
    agents_out: list[dict] = []
    for name in known_agents:
        last_sig = _latest_signal_for_agent(STATE.research_log, name, window_days)
        agents_out.append(
            _agent_stats(name, closed_by_agent.get(name, []), opens_by_agent.get(name, []), last_sig).to_dict()
        )
        seen.add(name)
    # Surface any unexpected agent name found in the data (e.g. ad-hoc tools).
    for name in set(closed_by_agent.keys()) | set(opens_by_agent.keys()):
        if name not in seen:
            last_sig = _latest_signal_for_agent(STATE.research_log, name, window_days)
            agents_out.append(
                _agent_stats(name, closed_by_agent.get(name, []), opens_by_agent.get(name, []), last_sig).to_dict()
            )

    # ---------- trades (full list, capped)
    trades_out = [_summarise_trade_for_payload(t) for t in (opens_all + closed_all)]

    return {
        "portfolio": portfolio,
        "agents": agents_out,
        "equity_curve": _equity_curve(closed_all),
        "distribution": {
            "win_loss_per_agent": _win_loss_per_agent(closed_all),
            "pnl_histogram": _pnl_histogram(closed_all),
            "hold_buckets": _hold_buckets(closed_all),
        },
        "correlation": _correlation_matrix(closed_all),
        "trades": trades_out,
        "ts": now.isoformat(),
    }


# ---------------------------------------------------------------- helpers


def _latest_signal_for_agent(rl, agent: str, window_days: int) -> str | None:
    for st in (
        "funding_rate", "regime", "sentiment_score", "last_close",
        "pairs_fit", "news_headline", "crypto_size_modifier",
    ):
        recs = rl.recent(st, window=timedelta(days=window_days), agent=agent, limit=1)
        if recs:
            ts = _aware(recs[0].ts)
            return ts.isoformat() if ts is not None else None
    return None
