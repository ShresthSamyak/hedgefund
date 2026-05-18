"""Telegram daily-digest formatter.

Reads the latest `daily_snapshot` Snapshot, renders a Telegram-friendly
plain-text message (no markdown to avoid escaping pain), and sends it via
the configured `TelegramTransport`. Stays well under the 4096-char limit
by truncating per-agent rows if needed.

When an LLM client is provided, prepends an LLM-narrated paragraph
("what happened today, which agents pulled their weight, what to watch
tomorrow"). Uses the `reasoning` tier — Gemini 3.1 Pro by default — so
the narrative is genuinely insightful, not just a re-statement of the
table. One call per digest run, ~$0.001/day at typical token volumes.

Pure logic split from transport so the formatter is unit-testable
without hitting Telegram or Vertex.
"""
from __future__ import annotations

import logging

from models.llm_client import LLMClient, NullLLM
from tools.daily_snapshot import Snapshot

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
TRUNC_NOTE = "\n... (truncated; see reports/ for full digest)"
NARRATIVE_MAX_CHARS = 600   # leaves room for the metric table


def build_narrative(snap: Snapshot, llm: LLMClient | None) -> str | None:
    """Ask the LLM for a 2-3 sentence color paragraph. Returns None on
    NullLLM, no agent activity, or any failure (digest still renders).
    """
    if llm is None or isinstance(llm, NullLLM):
        return None
    if not snap.per_agent:
        return None
    prompt = _build_prompt(snap)
    try:
        resp = llm.complete(prompt, tier="reasoning")
        text = (resp.text or "").strip()
    except Exception:
        log.exception("narrative LLM call failed")
        return None
    if not text:
        return None
    if len(text) > NARRATIVE_MAX_CHARS:
        text = text[: NARRATIVE_MAX_CHARS - 3] + "..."
    return text


def _build_prompt(snap: Snapshot) -> str:
    rows = []
    for agent, s in sorted(snap.per_agent.items()):
        rows.append(
            f"  {agent}: closed={s['trades_closed']} "
            f"wr={s['win_rate']*100:.0f}% pnl={s['pnl']:+.2f} "
            f"open={s['open_positions']}"
        )
    agents_block = "\n".join(rows) if rows else "  (no activity)"
    mode = "PAPER" if snap.paper_mode else "LIVE"
    kill = "KILL SWITCH ACTIVE\n" if snap.kill_switch_active else ""
    return (
        "You are a quant strategy reviewer summarising the last 24h for the "
        "AlphaGrid operator. Be precise, terse, and reference the actual numbers. "
        "Write 2-3 sentences, no bullet points, no preamble.\n\n"
        f"Mode: {mode}\n"
        f"Window: last {snap.window_hours}h\n"
        f"{kill}"
        f"Portfolio: pnl={snap.portfolio_pnl:+.2f}  "
        f"closed={snap.portfolio_trades_closed}  "
        f"sharpe30d={snap.running_sharpe_30d:+.2f}  "
        f"dd30d={snap.drawdown_30d:.2%}\n"
        f"Per-agent:\n{agents_block}\n\n"
        "Cover: (1) which agents pulled their weight and which dragged, "
        "(2) the single most important pattern you see, "
        "(3) one specific thing to watch in the next 24h. "
        "Mention numbers. No fluff. No questions back to the user."
    )


def format_digest(snap: Snapshot, narrative: str | None = None) -> str:
    """Compact, Telegram-friendly rendering of a daily snapshot.

    If `narrative` is provided, it's rendered at the top after the header
    so the operator sees the LLM color first on a phone notification.
    """
    lines: list[str] = []
    lines.append(f"AlphaGrid digest {snap.snapshot_ts}")
    mode = "PAPER" if snap.paper_mode else "LIVE"
    lines.append(f"mode={mode}  window={snap.window_hours}h")

    if narrative:
        lines.append("")
        lines.append(narrative)

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
    keep = TELEGRAM_LIMIT - len(TRUNC_NOTE)
    return body[:keep] + TRUNC_NOTE


def send_digest(
    snap: Snapshot,
    transport,
    *,
    llm: LLMClient | None = None,
) -> str:
    """Build the narrative (if llm provided), format, send. Returns the
    text actually sent.
    """
    narrative = build_narrative(snap, llm)
    text = format_digest(snap, narrative=narrative)
    transport.send(text)
    return text
