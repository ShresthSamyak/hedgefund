"use client";

import { Portfolio } from "@/lib/api";

const sign = (v: number) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));
const cls = (v: number) => (v > 0 ? "text-pos" : v < 0 ? "text-neg" : "text-text");
const pct = (v: number) => `${(v * 100).toFixed(2)}%`;

export function PortfolioBar({ data }: { data: Portfolio | null }) {
  if (!data) return <div className="h-16 bg-card border border-border rounded animate-pulse" />;
  return (
    <div className="bg-card border border-border rounded px-6 py-4 flex items-center gap-8">
      <div>
        <div className="text-xs text-muted uppercase tracking-wide">Paper mode</div>
        <div className="font-mono text-sm mt-1">
          {data.paper_mode ? <span className="text-warn">ON</span> : <span className="text-pos">LIVE</span>}
        </div>
      </div>
      <Divider />
      <Metric label="P&L (24h)" value={sign(data.pnl_today)} className={cls(data.pnl_today)} />
      <Metric label="P&L (30d)" value={sign(data.pnl_30d)} className={cls(data.pnl_30d)} />
      <Metric label="Sharpe (30d)" value={data.running_sharpe_30d.toFixed(2)} />
      <Metric
        label="Drawdown (30d)"
        value={pct(data.drawdown_30d)}
        className={data.drawdown_30d >= data.kill_switch_limit ? "text-neg" : "text-text"}
      />
      <Metric label="Open" value={String(data.open_positions)} />
      <Metric label="Trades (24h)" value={String(data.trades_closed_24h)} />
      <div className="ml-auto">
        {data.kill_switch_active ? (
          <span className="px-3 py-1 rounded bg-danger/20 text-danger font-mono text-xs uppercase">
            kill switch active
          </span>
        ) : (
          <span className="px-3 py-1 rounded bg-pos/10 text-pos font-mono text-xs uppercase">
            running
          </span>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value, className = "text-text" }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <div className="text-xs text-muted uppercase tracking-wide">{label}</div>
      <div className={`font-mono num text-lg mt-1 ${className}`}>{value}</div>
    </div>
  );
}

function Divider() {
  return <div className="h-10 w-px bg-border" />;
}
