"use client";

import { Line, LineChart, ResponsiveContainer } from "recharts";

import { PerformanceAgent } from "@/lib/api";

const sgn = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));
const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(0)}%`);
const fmtHold = (s: number | null) => {
  if (s === null) return "—";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
};

export function AgentPerformanceGrid({ agents }: { agents: PerformanceAgent[] }) {
  return (
    <section>
      <h3 className="text-xs uppercase tracking-wider text-muted mb-3">Agents</h3>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {agents.map((a) => (
          <AgentCard key={a.name} agent={a} />
        ))}
      </div>
    </section>
  );
}

function AgentCard({ agent: a }: { agent: PerformanceAgent }) {
  const isCrypto = a.name.includes("crypto") || a.name === "trading_funding" || a.name === "trading_trend";
  const isResearch = a.name.startsWith("research_");
  const border =
    a.status === "no_signal" ? "border-border" :
    a.total_pnl > 0 ? "border-pos/40" :
    a.total_pnl < 0 ? "border-neg/40" :
    "border-border";

  const sparkData = a.sparkline.map((v, i) => ({ i, v }));

  return (
    <div className={`bg-card border ${border} rounded p-4 flex flex-col gap-3`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="font-mono text-sm">{a.name}</div>
          <div className="flex gap-1 mt-1">
            <Badge label={isCrypto ? "CRYPTO" : isResearch ? "RESEARCH" : "EQUITY"} tone="neutral" />
            <Badge label={a.status.toUpperCase()} tone={statusTone(a.status)} />
          </div>
        </div>
        <div className={`font-mono num text-right text-base ${a.total_pnl > 0 ? "text-pos" : a.total_pnl < 0 ? "text-neg" : "text-muted"}`}>
          {a.trades > 0 ? sgn(a.total_pnl) : "—"}
        </div>
      </div>

      <div className="h-10">
        {sparkData.length >= 2 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={sparkData} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
              <Line
                type="monotone"
                dataKey="v"
                stroke={a.total_pnl >= 0 ? "#00c48c" : "#ff4d4f"}
                dot={false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full grid place-items-center text-[10px] text-muted">No history</div>
        )}
      </div>

      <div className="grid grid-cols-3 gap-2 text-[10px]">
        <Cell label="Win" value={pct(a.win_rate)} />
        <Cell label="Trades" value={String(a.trades)} />
        <Cell label="Sharpe" value={a.sharpe?.toFixed(2) ?? "—"} />
        <Cell label="Max DD" value={pct(a.max_drawdown)} />
        <Cell label="Avg hold" value={fmtHold(a.avg_hold_seconds)} />
        <Cell label="Open" value={String(a.open_positions)} />
      </div>

      <div className="grid grid-cols-2 gap-2 text-[10px] pt-2 border-t border-border">
        <BestWorst label="Best" trade={a.best_trade} positive />
        <BestWorst label="Worst" trade={a.worst_trade} positive={false} />
      </div>

      <div className="text-[10px] text-muted">
        {a.last_signal_ts
          ? `last signal: ${new Date(a.last_signal_ts).toLocaleString()}`
          : "no signal yet"}
      </div>
    </div>
  );
}

function statusTone(s: string): "good" | "bad" | "neutral" | "warn" {
  if (s === "running") return "good";
  if (s === "losing") return "bad";
  if (s === "killed") return "bad";
  if (s === "paused") return "warn";
  return "neutral";
}

function Badge({ label, tone }: { label: string; tone: "good" | "bad" | "neutral" | "warn" }) {
  const cls = {
    good: "bg-pos/10 text-pos",
    bad: "bg-neg/10 text-neg",
    neutral: "bg-bg text-muted border border-border",
    warn: "bg-warn/10 text-warn",
  }[tone];
  return (
    <span className={`px-1.5 py-0.5 rounded font-mono text-[9px] uppercase ${cls}`}>{label}</span>
  );
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-muted uppercase tracking-wider">{label}</div>
      <div className="font-mono mt-0.5">{value}</div>
    </div>
  );
}

function BestWorst({
  label,
  trade,
  positive,
}: {
  label: string;
  trade: { ticker: string; pnl: number } | null;
  positive: boolean;
}) {
  if (trade === null) {
    return (
      <div>
        <div className="text-muted uppercase tracking-wider">{label}</div>
        <div className="font-mono mt-0.5 text-muted">—</div>
      </div>
    );
  }
  return (
    <div>
      <div className="text-muted uppercase tracking-wider">{label}</div>
      <div className={`font-mono mt-0.5 ${positive ? "text-pos" : "text-neg"} truncate`}>
        {trade.ticker} {sgn(trade.pnl)}
      </div>
    </div>
  );
}
