"""AlphaGrid live dashboard (Streamlit).

Run with:
    streamlit run dashboard/app.py

Two views, switchable via the sidebar:
  * Live      — open positions, per-agent activity, latest research signals,
                running drawdown vs the kill-switch threshold.
  * Track     — cumulative P&L, per-agent stats (win rate, Sharpe, P&L),
                full closed-trade table.

Reads directly from the SQLite database; no Redis or other infra required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from config.settings import get_settings
from record.research_log import ResearchLog, SignalRecord
from record.track_record import TrackRecord

REFRESH_SECONDS = 10
REPO_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- data loaders

@st.cache_resource
def _tr() -> TrackRecord:
    return TrackRecord()


@st.cache_resource
def _rl() -> ResearchLog:
    return ResearchLog()


def _open_positions_df(tr: TrackRecord) -> pd.DataFrame:
    rows = []
    for t in tr.open_positions():
        rows.append({
            "trade_id": t.id[:8],
            "agent": t.agent,
            "market": t.market,
            "ticker": t.ticker,
            "side": t.side,
            "qty": t.qty,
            "entry_price": t.entry_price,
            "entry_ts": t.entry_ts,
            "reason": t.reason_text,
        })
    return pd.DataFrame(rows)


def _closed_trades_df(tr: TrackRecord, days: int = 30) -> pd.DataFrame:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for t in tr.closed_trades(since=since):
        rows.append({
            "agent": t.agent,
            "ticker": t.ticker,
            "side": t.side,
            "qty": t.qty,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "pnl": t.pnl,
            "entry_ts": t.entry_ts,
            "exit_ts": t.exit_ts,
        })
    return pd.DataFrame(rows)


def _research_recent_df(rl: ResearchLog, signal_type: str, hours: int = 48) -> pd.DataFrame:
    recs: list[SignalRecord] = rl.recent(signal_type, window=timedelta(hours=hours))
    if not recs:
        return pd.DataFrame()
    rows = [{
        "ts": r.ts, "agent": r.agent, "ticker": r.ticker,
        "value": r.value, "payload": r.payload,
    } for r in recs]
    return pd.DataFrame(rows)


def _per_agent_stats(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    grouped = closed.groupby("agent")
    out = grouped.agg(
        trades=("pnl", "count"),
        wins=("pnl", lambda s: (s > 0).sum()),
        losses=("pnl", lambda s: (s < 0).sum()),
        pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
    )
    out["win_rate"] = out.apply(
        lambda r: (r["wins"] / (r["wins"] + r["losses"])) if (r["wins"] + r["losses"]) else 0.0,
        axis=1,
    )
    return out.reset_index()


# ----------------------------------------------------------------- views

def view_live(tr: TrackRecord, rl: ResearchLog) -> None:
    st.subheader("Risk")
    dd = tr.drawdown(days=30)
    settings = get_settings()
    limit = settings.risk.kill_switch_drawdown
    cols = st.columns(3)
    cols[0].metric("30d drawdown", f"{dd:.2%}", delta=f"limit {limit:.0%}", delta_color="off")
    cols[1].metric("Open positions", len(tr.open_positions()))
    cols[2].metric("Paper mode", "ON" if settings.runtime.paper_mode else "OFF")
    if dd >= limit:
        st.error(f"KILL SWITCH active: drawdown {dd:.2%} ≥ limit {limit:.0%}")

    st.subheader("Open positions")
    open_df = _open_positions_df(tr)
    if open_df.empty:
        st.info("No open positions")
    else:
        st.dataframe(open_df, use_container_width=True, hide_index=True)

    st.subheader("Latest research signals (last 48h)")
    cols = st.columns(2)
    with cols[0]:
        st.caption("Funding rates")
        df = _research_recent_df(rl, "funding_rate", hours=48)
        if df.empty:
            st.write("none yet")
        else:
            st.dataframe(df[["ts", "ticker", "value"]].head(20), use_container_width=True, hide_index=True)
    with cols[1]:
        st.caption("Indian sentiment")
        df = _research_recent_df(rl, "sentiment_score", hours=48)
        if df.empty:
            st.write("none yet")
        else:
            st.dataframe(df[["ts", "ticker", "value"]].head(20), use_container_width=True, hide_index=True)

    st.subheader("Regime")
    regime = rl.latest("PORTFOLIO", "regime")
    modifier = rl.latest("PORTFOLIO", "crypto_size_modifier")
    cols = st.columns(2)
    if regime is not None:
        cols[0].metric("Crypto regime", regime.payload.get("regime", "?"), delta=f"score {regime.value:+.2f}")
    else:
        cols[0].metric("Crypto regime", "no data")
    if modifier is not None:
        cols[1].metric("Size modifier", f"{modifier.value:+.2f}",
                       delta=modifier.payload.get("explanation", ""))
    else:
        cols[1].metric("Size modifier", "no data")


def view_track_record(tr: TrackRecord) -> None:
    sharpe = tr.running_sharpe(days=30)
    dd = tr.drawdown(days=30)
    closed = _closed_trades_df(tr, days=90)

    cols = st.columns(3)
    cols[0].metric("Running Sharpe (30d)", f"{sharpe:.2f}")
    cols[1].metric("Max drawdown (30d)", f"{dd:.2%}")
    cols[2].metric("Closed trades (90d)", len(closed))

    if closed.empty:
        st.info("No closed trades yet")
        return

    st.subheader("Cumulative P&L")
    closed = closed.sort_values("exit_ts").reset_index(drop=True)
    closed["cum_pnl"] = closed["pnl"].cumsum()
    st.line_chart(closed.set_index("exit_ts")[["cum_pnl"]])

    st.subheader("Per-agent stats")
    st.dataframe(_per_agent_stats(closed), use_container_width=True, hide_index=True)

    st.subheader("Closed trades")
    st.dataframe(closed, use_container_width=True, hide_index=True)


# ----------------------------------------------------------------- entrypoint

def main() -> None:
    st.set_page_config(page_title="AlphaGrid", layout="wide")
    st.title("AlphaGrid")
    settings = get_settings()
    st.caption(f"DB: {settings.runtime.alphagrid_db_url}")

    view = st.sidebar.radio("View", ["Live", "Track record"], index=0)
    auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
    if auto_refresh:
        st.sidebar.caption(f"Refreshing every {REFRESH_SECONDS}s")

    tr = _tr()
    rl = _rl()

    if view == "Live":
        view_live(tr, rl)
    else:
        view_track_record(tr)

    if auto_refresh:
        # Streamlit re-runs from the top on each refresh.
        import time
        time.sleep(REFRESH_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
