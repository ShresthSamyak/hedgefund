"""Daily burn-in snapshot.

Run once per day (or on demand) to capture a one-line-per-agent summary
of the last 24h. Two outputs:

  reports/snapshots.jsonl   append-only, one JSON line per snapshot — grep / jq friendly
  reports/YYYY-MM-DD.txt    human-readable summary, overwritten each day

Use:
    python -m tools.daily_snapshot                   # write today's snapshot
    python -m tools.daily_snapshot --window-hours 168  # week summary
    python -m tools.daily_snapshot --reports-dir /tmp  # custom output dir

Grep examples:
    # Days where momentum was net-profitable.
    cat reports/snapshots.jsonl | jq 'select(.per_agent.trading_momentum.pnl > 0)'
    # All snapshots with kill_switch active.
    cat reports/snapshots.jsonl | jq 'select(.kill_switch_active)'
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import get_settings
from record.research_log import ResearchLog
from record.track_record import TrackRecord

log = logging.getLogger("alphagrid.snapshot")


# ----------------------------------------------------------------- data model


@dataclass
class AgentStats:
    agent: str
    trades_opened: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    open_positions: int = 0
    last_signal_ts: str | None = None

    @property
    def win_rate(self) -> float:
        denom = self.wins + self.losses
        return self.wins / denom if denom else 0.0

    def to_dict(self) -> dict:
        return {
            "trades_opened": self.trades_opened,
            "trades_closed": self.trades_closed,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "pnl": round(self.pnl, 2),
            "open_positions": self.open_positions,
            "last_signal_ts": self.last_signal_ts,
        }


@dataclass
class Snapshot:
    snapshot_ts: str
    window_hours: int
    per_agent: dict[str, dict] = field(default_factory=dict)
    portfolio_pnl: float = 0.0
    portfolio_trades_closed: int = 0
    running_sharpe_30d: float = 0.0
    drawdown_30d: float = 0.0
    kill_switch_active: bool = False
    paper_mode: bool = True

    def to_dict(self) -> dict:
        return {
            "snapshot_ts": self.snapshot_ts,
            "window_hours": self.window_hours,
            "per_agent": self.per_agent,
            "portfolio_pnl": round(self.portfolio_pnl, 2),
            "portfolio_trades_closed": self.portfolio_trades_closed,
            "running_sharpe_30d": round(self.running_sharpe_30d, 3),
            "drawdown_30d": round(self.drawdown_30d, 4),
            "kill_switch_active": self.kill_switch_active,
            "paper_mode": self.paper_mode,
        }


# ----------------------------------------------------------------- snapshot logic


def build_snapshot(
    tr: TrackRecord,
    rl: ResearchLog,
    *,
    window_hours: int = 24,
    now: datetime | None = None,
) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    closed = [t for t in tr.closed_trades(since=since)]
    open_positions = tr.open_positions()

    # Per-agent stats — collect everything in one pass.
    stats: dict[str, AgentStats] = {}
    for t in closed:
        s = stats.setdefault(t.agent, AgentStats(agent=t.agent))
        s.trades_closed += 1
        pnl = t.pnl or 0.0
        s.pnl += pnl
        if pnl > 0:
            s.wins += 1
        elif pnl < 0:
            s.losses += 1
    for t in open_positions:
        s = stats.setdefault(t.agent, AgentStats(agent=t.agent))
        s.open_positions += 1
        # Count as "opened in window" only if entry_ts falls inside.
        entry_ts = _aware(t.entry_ts)
        if entry_ts is not None and entry_ts >= since:
            s.trades_opened += 1

    # Tag last research-log activity per agent.
    for agent in list(stats.keys()):
        rec = _latest_research_for_agent(rl, agent, window_hours)
        if rec is not None:
            stats[agent].last_signal_ts = rec

    settings = get_settings()
    drawdown = tr.drawdown(days=settings.risk.kill_switch_window_days)
    kill = drawdown >= settings.risk.kill_switch_drawdown

    return Snapshot(
        snapshot_ts=now.isoformat(timespec="seconds"),
        window_hours=window_hours,
        per_agent={k: v.to_dict() for k, v in sorted(stats.items())},
        portfolio_pnl=sum(s.pnl for s in stats.values()),
        portfolio_trades_closed=sum(s.trades_closed for s in stats.values()),
        running_sharpe_30d=tr.running_sharpe(days=30),
        drawdown_30d=drawdown,
        kill_switch_active=kill,
        paper_mode=settings.runtime.paper_mode,
    )


def write_snapshot(snapshot: Snapshot, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = reports_dir / "snapshots.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot.to_dict(), default=str) + "\n")

    day = snapshot.snapshot_ts[:10]  # YYYY-MM-DD
    txt_path = reports_dir / f"{day}.txt"
    txt_path.write_text(_render_text(snapshot), encoding="utf-8")
    return jsonl_path, txt_path


def _render_text(snap: Snapshot) -> str:
    lines: list[str] = []
    lines.append(f"AlphaGrid daily snapshot - {snap.snapshot_ts}")
    lines.append(f"window: last {snap.window_hours}h    paper_mode: {snap.paper_mode}")
    lines.append("-" * 68)
    lines.append(
        f"  portfolio pnl: {snap.portfolio_pnl:>10.2f}    "
        f"closed: {snap.portfolio_trades_closed:>3}    "
        f"sharpe(30d): {snap.running_sharpe_30d:>5.2f}    "
        f"dd(30d): {snap.drawdown_30d:>6.2%}"
    )
    if snap.kill_switch_active:
        lines.append("  KILL SWITCH ACTIVE - new trades blocked until DD ages out of window")
    lines.append("-" * 68)
    lines.append(
        f"  {'agent':<24} {'opened':>6} {'closed':>6} {'wins':>5} {'loss':>5} "
        f"{'win%':>6} {'pnl':>10} {'open':>5}"
    )
    if not snap.per_agent:
        lines.append("  (no agent activity in window)")
    for agent, s in snap.per_agent.items():
        lines.append(
            f"  {agent:<24} {s['trades_opened']:>6} {s['trades_closed']:>6} "
            f"{s['wins']:>5} {s['losses']:>5} {s['win_rate']*100:>5.1f}% "
            f"{s['pnl']:>10.2f} {s['open_positions']:>5}"
        )
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- helpers


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _latest_research_for_agent(rl: ResearchLog, agent: str, window_hours: int) -> str | None:
    # We don't know which signal_type each agent uses, so scan a handful.
    candidates = [
        "funding_rate", "regime", "sentiment_score", "last_close", "pairs_fit",
        "news_headline", "crypto_size_modifier",
    ]
    latest_ts: datetime | None = None
    for st in candidates:
        recs = rl.recent(st, window=timedelta(hours=window_hours), agent=agent, limit=1)
        if not recs:
            continue
        ts = _aware(recs[0].ts)
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
    return latest_ts.isoformat(timespec="seconds") if latest_ts else None


# ----------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    tr = TrackRecord()
    rl = ResearchLog()
    snap = build_snapshot(tr, rl, window_hours=args.window_hours)
    jsonl_path, txt_path = write_snapshot(snap, args.reports_dir)
    print(_render_text(snap))
    print(f"appended: {jsonl_path}")
    print(f"summary : {txt_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
