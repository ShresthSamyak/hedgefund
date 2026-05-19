"use client";

import { PerformancePortfolio } from "@/lib/api";

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(2)}%`);
const sgn = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));
const cls = (v: number | null) =>
  v === null ? "text-muted" : v > 0 ? "text-pos" : v < 0 ? "text-neg" : "text-text";

export function PortfolioOverviewBar({ p }: { p: PerformancePortfolio }) {
  return (
    <section className="bg-card border border-border rounded">
      <div className="px-6 py-5 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-x-8 gap-y-4">
        <Stat label="Total P&L" value={sgn(p.total_pnl)} className={cls(p.total_pnl)} hero />
        <Stat label="Annualised" value={p.annualised_return === null ? "—" : pct(p.annualised_return)} className={cls(p.annualised_return)} />
        <Stat label="Sharpe (30d)" value={p.sharpe_30d?.toFixed(2) ?? "—"} />
        <Stat label="Sharpe (90d)" value={p.sharpe_90d?.toFixed(2) ?? "—"} />
        <Stat label="Sharpe (all)" value={p.sharpe_all?.toFixed(2) ?? "—"} />
        <Stat label="Max DD" value={pct(p.max_drawdown)} className={p.max_drawdown >= p.kill_switch_limit ? "text-neg" : "text-text"} />
        <Stat label="Win rate" value={pct(p.win_rate)} />
        <Stat label="Trades" value={String(p.total_trades)} />
      </div>
      <div className="px-6 pb-4 flex items-center gap-4 text-[10px] uppercase tracking-wider text-muted">
        <span>{p.days_running}d running</span>
        <span>•</span>
        <span>{p.open_positions} open</span>
        <span>•</span>
        <span>window {p.window_days}d</span>
        <span className="ml-auto">
          {p.paper_mode ? (
            <span className="px-2 py-0.5 rounded bg-warn/10 text-warn">PAPER MODE</span>
          ) : (
            <span className="px-2 py-0.5 rounded bg-pos/10 text-pos">LIVE</span>
          )}
        </span>
        {p.kill_switch_active && (
          <span className="px-2 py-0.5 rounded bg-danger/20 text-danger">KILL SWITCH ACTIVE</span>
        )}
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  className = "text-text",
  hero = false,
}: {
  label: string;
  value: string;
  className?: string;
  hero?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] text-muted uppercase tracking-wider">{label}</div>
      <div className={`font-mono num ${hero ? "text-2xl" : "text-base"} mt-1 ${className}`}>{value}</div>
    </div>
  );
}
