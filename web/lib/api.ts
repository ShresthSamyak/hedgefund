// REST + WebSocket helpers for the AlphaGrid backend.

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const WS = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000";

export type Portfolio = {
  paper_mode: boolean;
  pnl_today: number;
  pnl_30d: number;
  running_sharpe_30d: number;
  drawdown_30d: number;
  kill_switch_active: boolean;
  kill_switch_limit: number;
  open_positions: number;
  trades_closed_24h: number;
  ts: string;
};

export type AgentStat = {
  name: string;
  status: "running" | "losing" | "paused" | "killed";
  trades_24h: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl_24h: number;
  open_positions: number;
  last_signal_ts: string | null;
};

export type TradeRow = {
  id: string;
  entry_ts: string | null;
  exit_ts: string | null;
  agent: string;
  market: string;
  ticker: string;
  side: string;
  qty: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number | null;
  reason_text: string;
  paper: boolean;
  open: boolean;
};

export type EquityPoint = { ts: string; cumulative_pnl: number };

export type BusEvent = { channel: string; payload: unknown };

async function jsonGet<T>(path: string): Promise<T> {
  const resp = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return (await resp.json()) as T;
}

export const api = {
  portfolio: () => jsonGet<Portfolio>("/portfolio"),
  agents: () => jsonGet<{ agents: AgentStat[] }>("/agents"),
  trades: (limit = 50) =>
    jsonGet<{ open: TradeRow[]; closed: TradeRow[] }>(`/trades?limit=${limit}`),
  equity: (days = 30) =>
    jsonGet<{ series: EquityPoint[] }>(`/equity?days=${days}`),
};

export function connectLive(onEvent: (ev: BusEvent) => void): () => void {
  const url = `${WS}/live`;
  let ws: WebSocket | null = null;
  let closed = false;
  let backoff = 1000;

  const open = () => {
    ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 1000;
    };
    ws.onmessage = (msg) => {
      try {
        onEvent(JSON.parse(msg.data) as BusEvent);
      } catch {
        /* ignore non-json */
      }
    };
    ws.onclose = () => {
      if (closed) return;
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    ws.onerror = () => ws?.close();
  };
  open();

  return () => {
    closed = true;
    ws?.close();
  };
}
