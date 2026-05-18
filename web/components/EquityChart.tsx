"use client";

import { EquityPoint } from "@/lib/api";
import { LineChart, Line, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid } from "recharts";

export function EquityChart({ data }: { data: EquityPoint[] }) {
  const chartData = data.map((p) => ({
    t: new Date(p.ts).getTime(),
    pnl: p.cumulative_pnl,
  }));
  const last = chartData[chartData.length - 1]?.pnl ?? 0;
  return (
    <div className="bg-card border border-border rounded p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs text-muted uppercase tracking-wide">Cumulative P&amp;L (30d)</div>
        <div className={`font-mono num text-sm ${last > 0 ? "text-pos" : last < 0 ? "text-neg" : "text-text"}`}>
          {last > 0 ? `+${last.toFixed(2)}` : last.toFixed(2)}
        </div>
      </div>
      <div className="h-64">
        {chartData.length === 0 ? (
          <div className="h-full grid place-items-center text-muted text-xs">no closed trades in window</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="#222" strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                stroke="#666"
                tick={{ fontSize: 10 }}
              />
              <YAxis stroke="#666" tick={{ fontSize: 10 }} />
              <Tooltip
                contentStyle={{ background: "#111", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
                labelFormatter={(t) => new Date(Number(t)).toLocaleString()}
                formatter={(v: number) => [`${v > 0 ? "+" : ""}${v.toFixed(2)}`, "P&L"]}
              />
              <Line type="monotone" dataKey="pnl" stroke="#00c48c" dot={false} strokeWidth={1.5} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
