"use client";

import { useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { EquityCurvePoint } from "@/lib/api";

type Range = "1D" | "7D" | "30D" | "90D" | "ALL";
const RANGES: { label: Range; days: number | null }[] = [
  { label: "1D", days: 1 },
  { label: "7D", days: 7 },
  { label: "30D", days: 30 },
  { label: "90D", days: 90 },
  { label: "ALL", days: null },
];

// Deterministic color picker so each agent stays the same color across renders.
const AGENT_COLORS = [
  "#3b82f6", "#a78bfa", "#22d3ee", "#fb923c",
  "#f472b6", "#10b981", "#facc15", "#94a3b8",
];

function colorFor(agent: string, agents: string[]) {
  const idx = agents.indexOf(agent);
  return AGENT_COLORS[idx % AGENT_COLORS.length];
}

export function CumulativePnLChart({ data }: { data: EquityCurvePoint[] }) {
  const [range, setRange] = useState<Range>("ALL");

  const agents = useMemo(() => {
    if (data.length === 0) return [];
    const keys = Object.keys(data[data.length - 1]).filter(
      (k) => k !== "ts" && k !== "total",
    );
    return keys.sort();
  }, [data]);

  const filtered = useMemo(() => {
    if (data.length === 0) return [];
    const sel = RANGES.find((r) => r.label === range)!;
    if (sel.days === null) return data;
    const cutoff = Date.now() - sel.days * 24 * 3600 * 1000;
    return data.filter((p) => new Date(p.ts).getTime() >= cutoff);
  }, [data, range]);

  const chartData = useMemo(() => {
    let peak = 0;
    return filtered.map((p) => {
      peak = Math.max(peak, p.total);
      const drawdown = peak > 0 ? p.total - peak : 0;
      return { ...p, t: new Date(p.ts).getTime(), drawdown };
    });
  }, [filtered]);

  return (
    <section className="bg-card border border-border rounded p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-xs uppercase tracking-wider text-muted">Cumulative P&L</h3>
          <p className="text-[10px] text-muted mt-0.5">{agents.length} agents · {chartData.length} pts</p>
        </div>
        <div className="flex gap-1 text-[10px] font-mono">
          {RANGES.map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r.label)}
              className={
                "px-2 py-1 rounded transition-colors " +
                (range === r.label
                  ? "bg-bg text-text border border-border"
                  : "text-muted hover:text-text")
              }
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="h-96">
        {chartData.length === 0 ? (
          <div className="h-full grid place-items-center text-muted text-xs">
            No closed trades in this window
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
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
                contentStyle={{ background: "#0a0a0a", border: "1px solid #222", borderRadius: 4, fontSize: 11 }}
                labelFormatter={(t) => new Date(Number(t)).toLocaleString()}
                formatter={(v: number) =>
                  typeof v === "number" ? [`${v > 0 ? "+" : ""}${v.toFixed(2)}`, ""] : [v, ""]
                }
              />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              <Area
                type="monotone"
                dataKey="drawdown"
                stroke="none"
                fill="#ff4d4f"
                fillOpacity={0.12}
                isAnimationActive={false}
                legendType="none"
              />
              {agents.map((a) => (
                <Line
                  key={a}
                  type="monotone"
                  dataKey={a}
                  stroke={colorFor(a, agents)}
                  dot={false}
                  strokeWidth={1}
                  isAnimationActive={false}
                />
              ))}
              <Line
                type="monotone"
                dataKey="total"
                stroke="#e5e5e5"
                dot={false}
                strokeWidth={2}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}
