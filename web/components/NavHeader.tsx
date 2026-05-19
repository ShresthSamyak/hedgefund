"use client";

import Link from "next/link";

type Props = {
  active: "terminal" | "performance";
  rightStatus?: React.ReactNode;
};

export function NavHeader({ active, rightStatus }: Props) {
  return (
    <header className="border-b border-border bg-bg/60 backdrop-blur sticky top-0 z-10 px-6 py-3 flex items-baseline justify-between">
      <div className="flex items-baseline gap-6">
        <div>
          <div className="font-mono text-sm tracking-wider">ALPHAGRID</div>
          <div className="text-[10px] text-muted">autonomous trading terminal</div>
        </div>
        <nav className="flex items-center gap-1 ml-6 text-[10px] font-mono uppercase tracking-wider">
          <Tab href="/" label="terminal" active={active === "terminal"} />
          <Tab href="/performance" label="performance" active={active === "performance"} />
        </nav>
      </div>
      <div className="text-[10px] text-muted font-mono">{rightStatus}</div>
    </header>
  );
}

function Tab({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <Link
      href={href}
      className={
        "px-3 py-1 rounded transition-colors " +
        (active
          ? "bg-card text-text border border-border"
          : "text-muted hover:text-text hover:bg-card/50")
      }
    >
      {label}
    </Link>
  );
}
