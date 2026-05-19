"use client";

import { TradeRow } from "@/lib/api";

export function TradeFeed({ trades }: { trades: TradeRow[] }) {
  if (trades.length === 0) {
    return <div className="bg-card border border-border rounded p-4 text-muted text-sm">No trades yet.</div>;
  }
  return (
    <div className="bg-card border border-border rounded overflow-hidden">
      <div className="px-4 py-3 border-b border-border text-xs text-muted uppercase tracking-wide flex items-center justify-between">
        <span>Trade feed</span>
        <span className="font-mono">{trades.length}</span>
      </div>
      <div className="max-h-[640px] overflow-y-auto">
        <table className="w-full text-xs font-mono">
          <thead className="sticky top-0 bg-card text-muted uppercase tracking-wide text-[10px]">
            <tr>
              <th className="text-left px-4 py-2">Time</th>
              <th className="text-left px-4 py-2">Agent</th>
              <th className="text-left px-4 py-2">Side</th>
              <th className="text-left px-4 py-2">Ticker</th>
              <th className="text-right px-4 py-2">Qty</th>
              <th className="text-right px-4 py-2">Entry</th>
              <th className="text-right px-4 py-2">Exit</th>
              <th className="text-right px-4 py-2">P&amp;L</th>
              <th className="text-left px-4 py-2">Reason</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => {
              const isOpen = t.open;
              const pnl = t.pnl ?? 0;
              const pnlCls = pnl > 0 ? "text-pos" : pnl < 0 ? "text-neg" : "text-text";
              const sideCls = /BUY|LONG/.test(t.side) ? "text-pos" : "text-neg";
              return (
                <tr key={t.id} className="border-t border-border hover:bg-bg/50">
                  <td className="px-4 py-2 text-muted">
                    {new Date((t.exit_ts ?? t.entry_ts) || Date.now()).toLocaleTimeString()}
                  </td>
                  <td className="px-4 py-2">{t.agent}</td>
                  <td className={`px-4 py-2 ${sideCls}`}>{t.side}</td>
                  <td className="px-4 py-2">{t.ticker}</td>
                  <td className="px-4 py-2 text-right num">{formatQty(t.qty)}</td>
                  <td className="px-4 py-2 text-right num">{t.entry_price.toFixed(2)}</td>
                  <td className="px-4 py-2 text-right num">
                    {t.exit_price === null ? <span className="text-warn">open</span> : t.exit_price.toFixed(2)}
                  </td>
                  <td className={`px-4 py-2 text-right num ${isOpen ? "text-muted" : pnlCls}`}>
                    {isOpen ? "-" : (pnl > 0 ? `+${pnl.toFixed(2)}` : pnl.toFixed(2))}
                  </td>
                  <td className="px-4 py-2 max-w-[22rem]">
                    <div className="text-muted truncate" title={t.reason_text}>{t.reason_text}</div>
                    {t.llm_reason ? (
                      <div
                        className="text-accent/80 italic mt-0.5 truncate text-[10px]"
                        title={t.llm_reason}
                      >
                        ✶ {t.llm_reason}
                      </div>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
