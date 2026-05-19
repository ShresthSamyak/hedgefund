"use client";

import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EquityPoint } from "@/lib/api";

const SPAN_MS = 24 * 3600 * 1000;   // 24h threshold for "compressed" data

export function EquityChart({ data }: { data: EquityPoint[] }) {
  const chartData = data.map((p) => ({
    t: new Date(p.ts).getTime(),
    pnl: p.cumulative_pnl,
  }));
  const last = chartData[chartData.length - 1]?.pnl ?? 0;
  const lastCls =
    last > 0 ? "text-pos" : last < 0 ? "text-neg" : "text-text";

  // If we have <2 points OR all points are within 24h, dots beat lines —
  // a near-vertical line on a 30-day axis looks like an empty panel.
  const span =
    chartData.length >= 2
      ? chartData[chartData.length - 1].t - chartData[0].t
      : 0;
  const sparse = chartData.length < 2 || span < SPAN_MS;

  return (
    <div className="bg-card border border-border rounded p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs text-muted uppercase tracking-wide">Cumulative P&amp;L (30d)</div>
        <div className={`font-mono num text-sm ${lastCls}`}>
          {last > 0 ? `+${last.toFixed(2)}` : last.toFixed(2)}
        </div>
      </div>

      <div className="h-64">
        {chartData.length === 0 ? (
          <div className="h-full grid place-items-center text-muted text-xs">
            no closed trades in window
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="#222" strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t) =>
                  new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" })
                }
                stroke="#666"
                tick={{ fontSize: 10 }}
              />
              <YAxis stroke="#666" tick={{ fontSize: 10 }} />
              <Tooltip
                contentStyle={{ background: "#111", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
                labelFormatter={(t) => new Date(Number(t)).toLocaleString()}
                formatter={(v: number) => [`${v > 0 ? "+" : ""}${v.toFixed(2)}`, "P&L"]}
              />
              <Line
                type="monotone"
                dataKey="pnl"
                stroke={last >= 0 ? "#00c48c" : "#ff4d4f"}
                dot={sparse ? { r: 4, fill: last >= 0 ? "#00c48c" : "#ff4d4f", stroke: "#0a0a0a" } : false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {sparse && chartData.length > 0 && (
        <div className="mt-2 text-[10px] text-muted text-center">
          {chartData.length === 1
            ? "Only 1 closed trade — not enough to plot a curve. See Performance tab for full history."
            : `Only ${chartData.length} trades, all within a few hours — showing dots instead of a line.`}
        </div>
      )}
    </div>
  );
}
