"use client";

import { CorrelationData } from "@/lib/api";

export function CorrelationMatrix({ data }: { data: CorrelationData }) {
  return (
    <section>
      <div className="flex items-baseline gap-4 mb-3">
        <h3 className="text-xs uppercase tracking-wider text-muted">Strategy correlation</h3>
        {data && (
          <span className="text-[10px] text-muted">
            daily P&L · {data.n_days} days
          </span>
        )}
      </div>

      {data === null ? (
        <div className="bg-card border border-border rounded p-6 text-center text-muted text-xs">
          Need at least 2 agents with overlapping trades over 3+ days. Run for longer or trade more agents.
        </div>
      ) : (
        <div className="bg-card border border-border rounded p-4">
          <Matrix data={data} />
          <Legend />
        </div>
      )}
    </section>
  );
}

function Matrix({ data }: { data: NonNullable<CorrelationData> }) {
  const { agents, matrix } = data;
  return (
    <div className="overflow-x-auto">
      <table className="text-[10px] font-mono">
        <thead>
          <tr>
            <th className="p-2"></th>
            {agents.map((a) => (
              <th key={a} className="p-2 text-muted text-left whitespace-nowrap">
                {a.replace("trading_", "").replace("research_", "")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {agents.map((row, i) => (
            <tr key={row}>
              <th className="p-2 text-muted text-right whitespace-nowrap">
                {row.replace("trading_", "").replace("research_", "")}
              </th>
              {agents.map((_col, j) => (
                <td key={j} className="p-1">
                  <Cell r={matrix[i][j]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Cell({ r }: { r: number | null }) {
  if (r === null) {
    return (
      <div className="w-14 h-10 grid place-items-center bg-bg border border-border text-muted">
        —
      </div>
    );
  }
  const bg = corrColor(r);
  const txt = Math.abs(r) > 0.6 ? "text-bg" : "text-text";
  return (
    <div
      className={`w-14 h-10 grid place-items-center ${txt}`}
      style={{ background: bg }}
      title={`ρ = ${r.toFixed(3)}`}
    >
      {r.toFixed(2)}
    </div>
  );
}

// Diverging color scale: red (ρ→+1, correlated, BAD), green (ρ→0, good),
// blue (ρ→-1, anti-correlated, fine but unusual).
function corrColor(r: number): string {
  if (r > 0) {
    // 0 (green) -> +1 (red)
    const t = Math.min(1, r);
    const g = Math.round(0xc4 * (1 - t) + 0x4d * t);
    const r2 = Math.round(0x00 * (1 - t) + 0xff * t);
    const b = Math.round(0x8c * (1 - t) + 0x4f * t);
    return `rgba(${r2}, ${g}, ${b}, 0.55)`;
  }
  // 0 (green) -> -1 (blue)
  const t = Math.min(1, -r);
  const r2 = Math.round(0x00 * (1 - t) + 0x3b * t);
  const g = Math.round(0xc4 * (1 - t) + 0x82 * t);
  const b = Math.round(0x8c * (1 - t) + 0xf6 * t);
  return `rgba(${r2}, ${g}, ${b}, 0.55)`;
}

function Legend() {
  return (
    <div className="mt-4 flex items-center gap-4 text-[10px] font-mono text-muted">
      <LegendSwatch color="rgba(59,130,246,0.55)" label="ρ = -1 anti-corr" />
      <LegendSwatch color="rgba(0,196,140,0.55)" label="ρ = 0 uncorrelated" />
      <LegendSwatch color="rgba(255,77,79,0.55)" label="ρ = +1 correlated (risk)" />
    </div>
  );
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="inline-block w-4 h-4 rounded" style={{ background: color }} />
      <span>{label}</span>
    </div>
  );
}
