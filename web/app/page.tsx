"use client";

import { useEffect, useRef, useState } from "react";
import { AgentGrid } from "@/components/AgentGrid";
import { EquityChart } from "@/components/EquityChart";
import { NavHeader } from "@/components/NavHeader";
import { PortfolioBar } from "@/components/PortfolioBar";
import { TradeFeed } from "@/components/TradeFeed";
import {
  AgentStat,
  BusEvent,
  EquityPoint,
  Portfolio,
  TradeRow,
  api,
  connectLive,
} from "@/lib/api";

const POLL_MS = 8_000;
const PULSE_MS = 3_000;

export default function Page() {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [agents, setAgents] = useState<AgentStat[]>([]);
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [recentlyFired, setRecentlyFired] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const pulseTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // Initial + periodic REST refresh.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const [p, a, t, e] = await Promise.all([
          api.portfolio(),
          api.agents(),
          api.trades(50),
          api.equity(30),
        ]);
        if (cancelled) return;
        setError(null);
        setPortfolio(p);
        setAgents(a.agents);
        // open trades first, then most recent closed.
        setTrades([...t.open, ...t.closed].slice(0, 60));
        setEquity(e.series);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Live WebSocket — pulses agents and surfaces toast-style messages.
  useEffect(() => {
    const off = connectLive((ev: BusEvent) => {
      if (ev.channel.startsWith("trade.")) {
        const payload = ev.payload as { agent?: string } | undefined;
        if (payload?.agent) pulseAgent(payload.agent);
      }
    });
    return () => off();
  }, []);

  function pulseAgent(name: string) {
    setRecentlyFired((prev) => {
      const next = new Set(prev);
      next.add(name);
      return next;
    });
    const existing = pulseTimers.current.get(name);
    if (existing) clearTimeout(existing);
    pulseTimers.current.set(
      name,
      setTimeout(() => {
        setRecentlyFired((prev) => {
          const next = new Set(prev);
          next.delete(name);
          return next;
        });
        pulseTimers.current.delete(name);
      }, PULSE_MS),
    );
  }

  return (
    <div className="min-h-screen bg-bg">
      <NavHeader
        active="terminal"
        rightStatus={error ? <span className="text-neg">api error: {error}</span> : "connected"}
      />

      <main className="p-6 grid gap-6 max-w-[1600px] mx-auto">
        <PortfolioBar data={portfolio} />

        <div className="grid grid-cols-12 gap-6">
          <section className="col-span-12 lg:col-span-3">
            <h2 className="text-xs uppercase tracking-wide text-muted mb-2">Agents</h2>
            <AgentGrid agents={agents} recentlyFired={recentlyFired} />
          </section>

          <section className="col-span-12 lg:col-span-6">
            <h2 className="text-xs uppercase tracking-wide text-muted mb-2">Trades</h2>
            <TradeFeed trades={trades} />
          </section>

          <section className="col-span-12 lg:col-span-3">
            <h2 className="text-xs uppercase tracking-wide text-muted mb-2">Equity</h2>
            <EquityChart data={equity} />
          </section>
        </div>
      </main>
    </div>
  );
}
