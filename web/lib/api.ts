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
  status: "running" | "losing" | "paused" | "killed" | "no_signal";
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
  llm_reason: string | null;
  paper: boolean;
  open: boolean;
};

export type EquityPoint = { ts: string; cumulative_pnl: number };

export type BusEvent = { channel: string; payload: unknown };

// -------- performance page --------

export type PerformancePortfolio = {
  total_pnl: number;
  annualised_return: number | null;
  sharpe_30d: number | null;
  sharpe_90d: number | null;
  sharpe_all: number | null;
  max_drawdown: number;
  win_rate: number;
  total_trades: number;
  open_positions: number;
  days_running: number;
  paper_mode: boolean;
  kill_switch_active: boolean;
  kill_switch_limit: number;
  window_days: number;
};

export type PerformanceAgent = {
  name: string;
  status: "running" | "losing" | "no_signal" | "paused" | "killed";
  total_pnl: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  sharpe: number | null;
  max_drawdown: number;
  avg_hold_seconds: number | null;
  sparkline: number[];
  best_trade: { ticker: string; pnl: number } | null;
  worst_trade: { ticker: string; pnl: number } | null;
  last_signal_ts: string | null;
  open_positions: number;
};

export type EquityCurvePoint = {
  ts: string;
  total: number;
  [agent: string]: number | string;   // every agent that ever traded
};

export type PerfTrade = {
  id: string;
  ts: string;
  agent: string;
  ticker: string;
  side: string;
  qty: number;
  entry: number;
  exit: number | null;
  pnl: number | null;
  hold_seconds: number | null;
  reason: string;
  llm_reason: string | null;
  open: boolean;
};

export type CorrelationData = {
  agents: string[];
  matrix: (number | null)[][];
  n_days: number;
} | null;

export type PerformanceSummary = {
  portfolio: PerformancePortfolio;
  agents: PerformanceAgent[];
  equity_curve: EquityCurvePoint[];
  distribution: {
    win_loss_per_agent: { agent: string; wins: number; losses: number }[];
    pnl_histogram: { bucket: string; count: number }[];
    hold_buckets: { bucket: string; trades: number; wins: number; losses: number }[];
  };
  correlation: CorrelationData;
  trades: PerfTrade[];
  ts: string;
};

export const apiPerformance = {
  summary: (windowDays = 90) =>
    jsonGet<PerformanceSummary>(`/performance/summary?window_days=${windowDays}`),
};

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
