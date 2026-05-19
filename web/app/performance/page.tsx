"use client";

import { useEffect, useState } from "react";

import { NavHeader } from "@/components/NavHeader";
import { AgentPerformanceGrid } from "@/components/performance/AgentPerformanceGrid";
import { CorrelationMatrix } from "@/components/performance/CorrelationMatrix";
import { CumulativePnLChart } from "@/components/performance/CumulativePnLChart";
import { PortfolioOverviewBar } from "@/components/performance/PortfolioOverviewBar";
import { TradeDistributionCharts } from "@/components/performance/TradeDistributionCharts";
import { TradeLogTable } from "@/components/performance/TradeLogTable";
import { PerformanceSummary, apiPerformance } from "@/lib/api";

const POLL_MS = 60_000;

export default function PerformancePage() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await apiPerformance.summary(90);
        if (cancelled) return;
        setSummary(s);
        setError(null);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="min-h-screen bg-bg">
      <NavHeader
        active="performance"
        rightStatus={
          error
            ? <span className="text-neg">api error: {error}</span>
            : summary
              ? <span>updated {new Date(summary.ts).toLocaleTimeString()}</span>
              : "loading"
        }
      />

      <main className="p-6 grid gap-8 max-w-[1600px] mx-auto">
        {loading && summary === null ? (
          <SkeletonState />
        ) : summary === null ? (
          <ErrorState message={error ?? "unknown error"} />
        ) : (
          <>
            <PortfolioOverviewBar p={summary.portfolio} />
            <CumulativePnLChart data={summary.equity_curve} />
            <AgentPerformanceGrid agents={summary.agents} />
            <TradeDistributionCharts d={summary.distribution} />
            <CorrelationMatrix data={summary.correlation} />
            <TradeLogTable trades={summary.trades} />
          </>
        )}
      </main>
    </div>
  );
}

function SkeletonState() {
  return (
    <>
      <div className="h-32 bg-card border border-border rounded animate-pulse" />
      <div className="h-96 bg-card border border-border rounded animate-pulse" />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="h-56 bg-card border border-border rounded animate-pulse" />
        ))}
      </div>
    </>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="bg-card border border-neg/40 rounded p-6 text-center">
      <div className="text-neg font-mono text-sm mb-2">Failed to load performance data</div>
      <div className="text-muted text-xs">{message}</div>
      <div className="text-muted text-xs mt-4">
        Make sure the API is running: <code className="text-text">uvicorn api.main:app --port 8000</code>
      </div>
    </div>
  );
}
