"use client";

import { AgentStat } from "@/lib/api";

export function AgentGrid({ agents, recentlyFired }: { agents: AgentStat[]; recentlyFired: Set<string> }) {
  if (agents.length === 0) {
    return <div className="bg-card border border-border rounded p-4 text-muted text-sm">No agent activity in the last 24h.</div>;
  }
  return (
    <div className="grid grid-cols-1 gap-3">
      {agents.map((a) => (
        <AgentCard key={a.name} agent={a} pulse={recentlyFired.has(a.name)} />
      ))}
    </div>
  );
}

function AgentCard({ agent, pulse }: { agent: AgentStat; pulse: boolean }) {
  const sign = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));
  const pnlCls = agent.pnl_24h > 0 ? "text-pos" : agent.pnl_24h < 0 ? "text-neg" : "text-text";
  const losing = agent.status === "losing";
  return (
    <div
      className={`bg-card border rounded p-4 transition-colors ${
        losing ? "border-neg/40" : "border-border"
      } ${pulse ? "animate-pulse-blue" : ""}`}
    >
      <div className="flex items-center justify-between">
        <div className="font-mono text-sm">{agent.name}</div>
        <StatusBadge status={agent.status} />
      </div>
      <div className="grid grid-cols-3 gap-3 mt-3 text-xs">
        <Stat label="Trades" value={String(agent.trades_24h)} />
        <Stat label="Win rate" value={`${(agent.win_rate * 100).toFixed(0)}%`} />
        <Stat label="P&L" value={sign(agent.pnl_24h)} className={`font-mono ${pnlCls}`} />
      </div>
      <div className="mt-3 flex items-center justify-between text-[10px] text-muted">
        <span>Open: {agent.open_positions}</span>
        <span>{agent.last_signal_ts ? new Date(agent.last_signal_ts).toLocaleTimeString() : "no signal yet"}</span>
      </div>
    </div>
  );
}

function Stat({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <div className="text-muted uppercase tracking-wide text-[10px]">{label}</div>
      <div className={`mt-0.5 font-mono ${className || "text-text"}`}>{value}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: AgentStat["status"] }) {
  const map: Record<AgentStat["status"], string> = {
    running: "bg-pos/10 text-pos",
    losing: "bg-neg/10 text-neg",
    paused: "bg-warn/10 text-warn",
    killed: "bg-danger/20 text-danger",
  };
  return (
    <span className={`px-2 py-0.5 rounded font-mono text-[10px] uppercase ${map[status]}`}>{status}</span>
  );
}
