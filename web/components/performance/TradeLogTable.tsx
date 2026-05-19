"use client";

import { useMemo, useState } from "react";

import { PerfTrade } from "@/lib/api";

type SortKey =
  | "ts" | "agent" | "ticker" | "side" | "qty"
  | "entry" | "exit" | "pnl" | "hold_seconds";

type Direction = "asc" | "desc";

export function TradeLogTable({ trades }: { trades: PerfTrade[] }) {
  const [agent, setAgent] = useState<string>("all");
  const [side, setSide] = useState<string>("all");
  const [outcome, setOutcome] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("ts");
  const [direction, setDirection] = useState<Direction>("desc");

  const agents = useMemo(
    () => Array.from(new Set(trades.map((t) => t.agent))).sort(),
    [trades],
  );

  const filtered = useMemo(() => {
    let out = trades;
    if (agent !== "all") out = out.filter((t) => t.agent === agent);
    if (side !== "all") out = out.filter((t) => t.side.toUpperCase() === side);
    if (outcome === "win") out = out.filter((t) => (t.pnl ?? 0) > 0);
    if (outcome === "loss") out = out.filter((t) => (t.pnl ?? 0) < 0);
    if (outcome === "open") out = out.filter((t) => t.open);
    if (query.trim()) {
      const q = query.toLowerCase();
      out = out.filter(
        (t) =>
          t.ticker.toLowerCase().includes(q) ||
          t.agent.toLowerCase().includes(q) ||
          t.reason.toLowerCase().includes(q),
      );
    }
    out = [...out].sort((a, b) => {
      const av = (a as Record<string, unknown>)[sortKey];
      const bv = (b as Record<string, unknown>)[sortKey];
      if (av === null || av === undefined) return direction === "asc" ? -1 : 1;
      if (bv === null || bv === undefined) return direction === "asc" ? 1 : -1;
      if (typeof av === "number" && typeof bv === "number") {
        return direction === "asc" ? av - bv : bv - av;
      }
      return direction === "asc"
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    return out;
  }, [trades, agent, side, outcome, query, sortKey, direction]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setDirection(direction === "asc" ? "desc" : "asc");
    else {
      setSortKey(k);
      setDirection("desc");
    }
  }

  function exportCsv() {
    const cols: (keyof PerfTrade)[] = [
      "ts", "agent", "ticker", "side", "qty", "entry", "exit", "pnl",
      "hold_seconds", "open", "reason", "llm_reason",
    ];
    const head = cols.join(",");
    const lines = filtered.map((t) =>
      cols.map((c) => csvCell(t[c])).join(","),
    );
    const blob = new Blob([head + "\n" + lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `alphagrid-trades-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <section>
      <div className="flex items-center justify-between mb-3 gap-2">
        <h3 className="text-xs uppercase tracking-wider text-muted">
          Trade log <span className="text-text font-mono ml-2">{filtered.length}</span>
          <span className="text-muted text-[10px] ml-1">/ {trades.length}</span>
        </h3>
        <button
          onClick={exportCsv}
          disabled={filtered.length === 0}
          className="px-3 py-1 rounded bg-card border border-border text-[10px] font-mono uppercase tracking-wider hover:text-text text-muted disabled:opacity-40"
        >
          Export CSV
        </button>
      </div>

      <div className="bg-card border border-border rounded overflow-hidden">
        <div className="px-3 py-2 border-b border-border flex flex-wrap items-center gap-2 text-[10px] font-mono">
          <Select label="Agent" value={agent} onChange={setAgent}
                  options={[["all", "all"], ...agents.map((a) => [a, a]) as [string, string][]]} />
          <Select label="Side" value={side} onChange={setSide}
                  options={[["all", "all"], ["BUY", "BUY"], ["SHORT", "SHORT"], ["LONG", "LONG"], ["SELL", "SELL"]]} />
          <Select label="Outcome" value={outcome} onChange={setOutcome}
                  options={[["all", "all"], ["win", "wins"], ["loss", "losses"], ["open", "open"]]} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search ticker / agent / reason"
            className="ml-auto bg-bg border border-border rounded px-2 py-1 w-64 outline-none focus:border-accent text-[10px]"
          />
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono">
            <thead className="bg-card text-muted uppercase tracking-wider text-[9px] sticky top-0">
              <tr>
                <Th sortKey="ts" current={sortKey} dir={direction} onClick={toggleSort}>Time</Th>
                <Th sortKey="agent" current={sortKey} dir={direction} onClick={toggleSort}>Agent</Th>
                <Th sortKey="ticker" current={sortKey} dir={direction} onClick={toggleSort}>Ticker</Th>
                <Th sortKey="side" current={sortKey} dir={direction} onClick={toggleSort}>Side</Th>
                <Th sortKey="qty" current={sortKey} dir={direction} onClick={toggleSort} right>Qty</Th>
                <Th sortKey="entry" current={sortKey} dir={direction} onClick={toggleSort} right>Entry</Th>
                <Th sortKey="exit" current={sortKey} dir={direction} onClick={toggleSort} right>Exit</Th>
                <Th sortKey="pnl" current={sortKey} dir={direction} onClick={toggleSort} right>P&L</Th>
                <Th sortKey="hold_seconds" current={sortKey} dir={direction} onClick={toggleSort} right>Hold</Th>
                <th className="px-3 py-2 text-left">Reason</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={10} className="px-3 py-8 text-muted text-center">
                    No trades match these filters.
                  </td>
                </tr>
              ) : (
                filtered.map((t) => <Row key={t.id} t={t} />)
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Th({
  children,
  sortKey,
  current,
  dir,
  onClick,
  right = false,
}: {
  children: React.ReactNode;
  sortKey: SortKey;
  current: SortKey;
  dir: Direction;
  onClick: (k: SortKey) => void;
  right?: boolean;
}) {
  const arrow = current === sortKey ? (dir === "asc" ? "▲" : "▼") : "";
  return (
    <th
      className={`px-3 py-2 cursor-pointer select-none hover:text-text ${right ? "text-right" : "text-left"}`}
      onClick={() => onClick(sortKey)}
    >
      {children} <span className="text-accent">{arrow}</span>
    </th>
  );
}

function Row({ t }: { t: PerfTrade }) {
  const pnl = t.pnl ?? 0;
  const pnlCls = t.open ? "text-muted" : pnl > 0 ? "text-pos" : pnl < 0 ? "text-neg" : "text-text";
  const sideCls = /BUY|LONG/.test(t.side) ? "text-pos" : "text-neg";
  return (
    <tr className="border-t border-border hover:bg-bg/40">
      <td className="px-3 py-2 text-muted whitespace-nowrap">
        {new Date(t.ts).toLocaleString()}
      </td>
      <td className="px-3 py-2">{t.agent}</td>
      <td className="px-3 py-2">{t.ticker}</td>
      <td className={`px-3 py-2 ${sideCls}`}>{t.side}</td>
      <td className="px-3 py-2 text-right num">{fmtQty(t.qty)}</td>
      <td className="px-3 py-2 text-right num">{t.entry.toFixed(2)}</td>
      <td className="px-3 py-2 text-right num">
        {t.exit === null ? <span className="text-warn">open</span> : t.exit.toFixed(2)}
      </td>
      <td className={`px-3 py-2 text-right num ${pnlCls}`}>
        {t.open ? "—" : pnl > 0 ? `+${pnl.toFixed(2)}` : pnl.toFixed(2)}
      </td>
      <td className="px-3 py-2 text-right num">{fmtHold(t.hold_seconds)}</td>
      <td className="px-3 py-2 max-w-[24rem]">
        <div className="text-muted truncate" title={t.reason}>{t.reason}</div>
        {t.llm_reason ? (
          <div
            className="text-accent/80 italic mt-0.5 truncate text-[9px]"
            title={t.llm_reason}
          >
            ✶ {t.llm_reason}
          </div>
        ) : null}
      </td>
    </tr>
  );
}

function fmtQty(qty: number): string {
  if (qty === 0) return "0";
  const abs = Math.abs(qty);
  if (abs >= 1) return qty.toFixed(4).replace(/\.?0+$/, "");
  if (abs >= 0.01) return qty.toFixed(6).replace(/\.?0+$/, "");
  return qty.toPrecision(4);
}

function fmtHold(s: number | null): string {
  if (s === null) return "—";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

function csvCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  // RFC4180 quoting if needed
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-muted uppercase tracking-wider">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-bg border border-border rounded px-2 py-0.5 outline-none focus:border-accent"
      >
        {options.map(([v, label]) => (
          <option key={v} value={v}>{label}</option>
        ))}
      </select>
    </label>
  );
}
