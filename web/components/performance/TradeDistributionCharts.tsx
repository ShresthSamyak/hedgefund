"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { PerformanceSummary } from "@/lib/api";

type Distribution = PerformanceSummary["distribution"];

export function TradeDistributionCharts({ d }: { d: Distribution }) {
  return (
    <section>
      <h3 className="text-xs uppercase tracking-wider text-muted mb-3">Distribution</h3>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Panel title="Win / Loss per agent">
          <WinLossChart rows={d.win_loss_per_agent} />
        </Panel>
        <Panel title="P&L distribution">
          <PnLHistogram rows={d.pnl_histogram} />
        </Panel>
        <Panel title="Hold duration vs outcome">
          <HoldBuckets rows={d.hold_buckets} />
        </Panel>
      </div>
    </section>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded p-4">
      <div className="text-[10px] uppercase tracking-wider text-muted mb-3">{title}</div>
      <div className="h-56">{children}</div>
    </div>
  );
}

function Empty() {
  return <div className="h-full grid place-items-center text-muted text-xs">No data yet</div>;
}

function WinLossChart({ rows }: { rows: Distribution["win_loss_per_agent"] }) {
  if (rows.length === 0) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid stroke="#222" strokeDasharray="3 3" />
        <XAxis
          dataKey="agent"
          stroke="#666"
          tick={{ fontSize: 9 }}
          tickFormatter={(v: string) => v.replace("trading_", "").replace("research_", "")}
        />
        <YAxis stroke="#666" tick={{ fontSize: 10 }} allowDecimals={false} />
        <Tooltip
          contentStyle={{ background: "#0a0a0a", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
        />
        <Legend wrapperStyle={{ fontSize: 10 }} />
        <Bar dataKey="wins" stackId="a" fill="#00c48c" isAnimationActive={false} />
        <Bar dataKey="losses" stackId="a" fill="#ff4d4f" isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function PnLHistogram({ rows }: { rows: Distribution["pnl_histogram"] }) {
  if (rows.length === 0) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid stroke="#222" strokeDasharray="3 3" />
        <XAxis dataKey="bucket" stroke="#666" tick={{ fontSize: 9 }} />
        <YAxis stroke="#666" tick={{ fontSize: 10 }} allowDecimals={false} />
        <Tooltip
          contentStyle={{ background: "#0a0a0a", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
        />
        <Bar dataKey="count" isAnimationActive={false}>
          {rows.map((r, i) => (
            <Cell key={i} fill={parseFloat(r.bucket) >= 0 ? "#00c48c" : "#ff4d4f"} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function HoldBuckets({ rows }: { rows: Distribution["hold_buckets"] }) {
  if (rows.length === 0) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid stroke="#222" strokeDasharray="3 3" />
        <XAxis dataKey="bucket" stroke="#666" tick={{ fontSize: 10 }} />
        <YAxis stroke="#666" tick={{ fontSize: 10 }} allowDecimals={false} />
        <Tooltip
          contentStyle={{ background: "#0a0a0a", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
        />
        <Legend wrapperStyle={{ fontSize: 10 }} />
        <Bar dataKey="wins" stackId="a" fill="#00c48c" isAnimationActive={false} />
        <Bar dataKey="losses" stackId="a" fill="#ff4d4f" isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
