import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { ModeBanner } from "@/components/ModeBanner";
import { SessionBar } from "@/components/Session";

import "./globals.css";

export const metadata: Metadata = {
  title: "Rhythm AI Trader",
  description: "Research and PAPER trading dashboard. Live trading is disabled.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ModeBanner />
        <SessionBar>
          <header className="nav">
            <span className="brand">Rhythm AI Trader</span>
            <nav aria-label="Screens">
              <Link href="/">Portfolio dashboard</Link>
              <Link href="/intraday">Intraday</Link>
              <Link href="/swing">Swing</Link>
              <Link href="/long-term">Long-term</Link>
              <Link href="/research">AI research</Link>
              <Link href="/risk">Risk centre</Link>
              <Link href="/automation">Automation</Link>
              <Link href="/audit">Audit &amp; reconciliation</Link>
            </nav>
            <nav aria-label="More" className="nav-more">
              <Link href="/approvals">Approvals</Link>
              <Link href="/portfolio">Ledgers</Link>
              <Link href="/backtests">Backtests</Link>
              <Link href="/readiness">Live readiness</Link>
            </nav>
          </header>
          <main>{children}</main>
        </SessionBar>
        <footer className="foot">
          Decision support and PAPER simulation only. No profit is implied; nothing here is
          investment advice.
        </footer>
      </body>
    </html>
  );
}
