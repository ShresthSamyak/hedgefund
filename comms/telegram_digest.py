"""Telegram daily-digest formatter.

Reads the latest `daily_snapshot` Snapshot, renders a Telegram-friendly
plain-text message (no markdown to avoid escaping pain), and sends it via
the configured `TelegramTransport`. Stays well under the 4096-char limit
by truncating per-agent rows if needed.

Pure logic split from transport so the formatter is unit-testable without
hitting Telegram.
"""
from __future__ import annotations

import logging

from tools.daily_snapshot import Snapshot

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
TRUNC_NOTE = "\n... (truncated; see reports/ for full digest)"


def format_digest(snap: Snapshot) -> str:
    """Compact, Telegram-friendly rendering of a daily snapshot."""
    lines: list[str] = []
    lines.append(f"AlphaGrid digest {snap.snapshot_ts}")
    mode = "PAPER" if snap.paper_mode else "LIVE"
    lines.append(f"mode={mode}  window={snap.window_hours}h")
    lines.append("")
    pnl_sign = "+" if snap.portfolio_pnl > 0 else ""
    lines.append(
        f"pnl: {pnl_sign}{snap.portfolio_pnl:.2f}  "
        f"closed: {snap.portfolio_trades_closed}  "
        f"sharpe(30d): {snap.running_sharpe_30d:+.2f}  "
        f"dd(30d): {snap.drawdown_30d:.2%}"
    )
    if snap.kill_switch_active:
        lines.append("⚠ KILL SWITCH ACTIVE — new trades blocked")

    if snap.per_agent:
        lines.append("")
        lines.append("agents:")
        for name, s in snap.per_agent.items():
            lines.append(
                f"  {name}: closed={s['trades_closed']} "
                f"wr={s['win_rate']*100:.0f}% pnl={s['pnl']:+.2f} "
                f"open={s['open_positions']}"
            )
    else:
        lines.append("")
        lines.append("no agent activity in window")

    body = "\n".join(lines)
    if len(body) <= TELEGRAM_LIMIT:
        return body
    # Truncate while keeping the header + note marker.
    keep = TELEGRAM_LIMIT - len(TRUNC_NOTE)
    return body[:keep] + TRUNC_NOTE


def send_digest(snap: Snapshot, transport) -> str:
    """Render + send. Returns the text actually sent."""
    text = format_digest(snap)
    transport.send(text)
    return text
