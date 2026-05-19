"""FastAPI backend for the Next.js dashboard.

Three REST endpoints + one WebSocket:

  GET  /portfolio        top-bar metrics (pnl, sharpe, drawdown, kill switch)
  GET  /agents           per-agent grid data
  GET  /trades?limit=N   recent trades for the live feed
  WS   /live             broadcasts every signal_bus event in real time

Production path: this app subscribes to the same `InMemoryBus` the
trading scheduler uses (via the `attach_bus(bus)` hook). When a research
or trading agent publishes a signal, every connected dashboard sees it
within a few ms.

CORS is wide-open — the dashboard is read-only and exposes no secrets.
Lock down via Nginx + a firewall rule in production if needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config.settings import get_settings
from infra.signal_bus import SignalBus
from record.research_log import ResearchLog
from record.track_record import TrackRecord, Trade

log = logging.getLogger("alphagrid.api")


# --------------------------------------------------------- websocket fan-out


class WebSocketBroadcaster:
    """Tracks connected websockets and broadcasts JSON payloads to all."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def fanout(self, channel: str, payload: Any) -> None:
        message = json.dumps({"channel": channel, "payload": payload}, default=str)
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in self._clients:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# --------------------------------------------------------- app + state


class AppState:
    """Holds the shared track_record, research_log, bus, broadcaster."""

    def __init__(self) -> None:
        self.track_record = TrackRecord()
        self.research_log = ResearchLog()
        self.broadcaster = WebSocketBroadcaster()
        self.bus: SignalBus | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach_bus(self, bus: SignalBus) -> None:
        """Subscribe the broadcaster to every channel we care about."""
        self.bus = bus
        for ch in (
            "price.BTC/USDT", "price.ETH/USDT", "price.SOL/USDT",
            "news.alert", "news.raw",
            "research.regime", "research.size_modifier",
            "trade.opened", "trade.closed",
        ):
            bus.subscribe(ch, self._on_bus_event)

    def _on_bus_event(self, channel: str, payload: Any) -> None:
        loop = self.loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcaster.fanout(channel, payload), loop,
            )
        except RuntimeError:
            log.debug("event loop closed; dropping %s", channel)


STATE = AppState()


def _maybe_attach_redis_bus(state: AppState) -> None:
    """When deployed as a separate process from `main.py`, the api can
    still receive live events by subscribing to a shared Redis pub/sub bus.
    Skips silently if redis is unavailable.
    """
    settings = get_settings()
    url = settings.runtime.redis_url
    if not url or url.startswith("redis://127.0.0.1") and not _redis_reachable(url):
        return
    try:
        from infra.signal_bus import RedisBus
        bus = RedisBus(url)
        state.attach_bus(bus)
        log.info("api attached to RedisBus %s", url)
    except Exception:
        log.warning("redis bus unavailable; live websocket will only fan out "
                    "events published in this process")


def _redis_reachable(url: str) -> bool:
    try:
        import redis
        r = redis.from_url(url, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:
        return False


app = FastAPI(title="AlphaGrid API", version="0.1.0")

from api.performance import router as performance_router  # noqa: E402

app.include_router(performance_router)


@app.on_event("startup")
def _on_startup() -> None:
    STATE.loop = asyncio.get_event_loop()
    _maybe_attach_redis_bus(STATE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------- helpers


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _trade_to_dict(t: Trade) -> dict[str, Any]:
    entry_aware = _aware(t.entry_ts)
    exit_aware = _aware(t.exit_ts)
    payload = t.signal_payload or {}
    return {
        "id": t.id,
        "entry_ts": entry_aware.isoformat() if entry_aware is not None else None,
        "exit_ts": exit_aware.isoformat() if exit_aware is not None else None,
        "agent": t.agent,
        "market": t.market,
        "ticker": t.ticker,
        "side": t.side,
        "qty": t.qty,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl": t.pnl,
        "reason_text": t.reason_text,
        "llm_reason": payload.get("llm_reason"),
        "paper": bool(t.paper),
        "open": t.exit_ts is None,
    }


# --------------------------------------------------------- routes


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ws_clients": STATE.broadcaster.client_count,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/portfolio")
def portfolio() -> dict[str, Any]:
    settings = get_settings()
    tr = STATE.track_record
    dd = tr.drawdown(days=settings.risk.kill_switch_window_days)
    sharpe = tr.running_sharpe(days=30)
    closed_30d = tr.closed_trades(since=datetime.now(timezone.utc) - timedelta(days=30))
    pnl_30d = sum((t.pnl or 0.0) for t in closed_30d)
    closed_today = tr.closed_trades(since=datetime.now(timezone.utc) - timedelta(hours=24))
    pnl_today = sum((t.pnl or 0.0) for t in closed_today)
    return {
        "paper_mode": settings.runtime.paper_mode,
        "pnl_today": round(pnl_today, 2),
        "pnl_30d": round(pnl_30d, 2),
        "running_sharpe_30d": round(sharpe, 3),
        "drawdown_30d": round(dd, 4),
        "kill_switch_active": dd >= settings.risk.kill_switch_drawdown,
        "kill_switch_limit": settings.risk.kill_switch_drawdown,
        "open_positions": len(tr.open_positions()),
        "trades_closed_24h": len(closed_today),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


KNOWN_AGENTS = (
    "research_india", "research_crypto",
    "trading_funding", "trading_momentum", "trading_sentiment",
    "trading_pairs", "trading_trend", "trading_crypto_sent",
)


def _empty_agent_row(name: str) -> dict[str, Any]:
    return {
        "name": name, "trades_24h": 0, "wins": 0, "losses": 0,
        "pnl_24h": 0.0, "open_positions": 0, "last_signal_ts": None,
        "win_rate": 0.0, "status": "no_signal",
    }


@app.get("/agents")
def agents() -> dict[str, Any]:
    """Per-agent stats over the last 24h plus latest research signal.

    Every known agent is returned, even with zero activity, so the
    dashboard can render empty cards rather than hide them. This matches
    the performance page's behavior.
    """
    tr = STATE.track_record
    rl = STATE.research_log
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    closed = tr.closed_trades(since=since)
    opens = tr.open_positions()

    by_agent: dict[str, dict[str, Any]] = {n: _empty_agent_row(n) for n in KNOWN_AGENTS}

    for t in closed:
        s = by_agent.setdefault(t.agent, _empty_agent_row(t.agent))
        s["trades_24h"] += 1
        s["pnl_24h"] += t.pnl or 0.0
        if (t.pnl or 0.0) > 0:
            s["wins"] += 1
        elif (t.pnl or 0.0) < 0:
            s["losses"] += 1
    for t in opens:
        s = by_agent.setdefault(t.agent, _empty_agent_row(t.agent))
        s["open_positions"] += 1

    # Pull last research signal timestamp per agent.
    for name in by_agent:
        ts = _latest_signal_for_agent(rl, name)
        if ts is not None:
            by_agent[name]["last_signal_ts"] = ts

    for s in by_agent.values():
        denom = s["wins"] + s["losses"]
        s["win_rate"] = (s["wins"] / denom) if denom else 0.0
        s["pnl_24h"] = round(s["pnl_24h"], 2)
        if s["trades_24h"] == 0 and s["open_positions"] == 0:
            s["status"] = "no_signal"
        elif s["pnl_24h"] < 0:
            s["status"] = "losing"
        else:
            s["status"] = "running"
    return {"agents": sorted(by_agent.values(), key=lambda x: x["name"])}


@app.get("/trades")
def trades(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    tr = STATE.track_record
    closed = tr.closed_trades(limit=limit)
    opens = tr.open_positions()
    return {
        "open": [_trade_to_dict(t) for t in opens],
        "closed": [_trade_to_dict(t) for t in closed],
    }


@app.get("/equity")
def equity(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Cumulative equity curve for the chart panel."""
    tr = STATE.track_record
    since = datetime.now(timezone.utc) - timedelta(days=days)
    closed = sorted(
        tr.closed_trades(since=since),
        key=lambda t: _aware(t.exit_ts) or datetime.min.replace(tzinfo=timezone.utc),
    )
    series: list[dict[str, Any]] = []
    cumulative = 0.0
    for t in closed:
        cumulative += t.pnl or 0.0
        ts = _aware(t.exit_ts)
        if ts is None:
            continue
        series.append({"ts": ts.isoformat(), "cumulative_pnl": round(cumulative, 2)})
    return {"series": series}


@app.websocket("/live")
async def live(ws: WebSocket) -> None:
    # First connection captures the loop so the bus subscriber can fan out.
    if STATE.loop is None:
        STATE.loop = asyncio.get_running_loop()
    await STATE.broadcaster.add(ws)
    try:
        # Send an initial hello so the client can confirm.
        await ws.send_text(json.dumps({"channel": "hello", "payload": {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ws_clients": STATE.broadcaster.client_count,
        }}))
        while True:
            # We don't expect client -> server messages; just keep the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await STATE.broadcaster.remove(ws)


# --------------------------------------------------------- helpers


def _latest_signal_for_agent(rl: ResearchLog, agent: str) -> str | None:
    candidates = (
        "funding_rate", "regime", "sentiment_score", "last_close",
        "pairs_fit", "news_headline", "crypto_size_modifier",
    )
    best: datetime | None = None
    for st in candidates:
        recs = rl.recent(st, window=timedelta(hours=24), agent=agent, limit=1)
        if not recs:
            continue
        ts = _aware(recs[0].ts)
        if ts is not None and (best is None or ts > best):
            best = ts
    return best.isoformat() if best else None
