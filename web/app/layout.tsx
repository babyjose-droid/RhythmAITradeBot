import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { ModeBanner } from "@/components/ModeBanner";

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
        <header className="nav">
          <span className="brand">Rhythm AI Trader</span>
          <nav>
            <Link href="/">Overview</Link>
            <Link href="/intraday">Intraday workspace</Link>
            <Link href="/portfolio">Portfolio &amp; reconciliation</Link>
            <Link href="/risk">Risk &amp; controls</Link>
            <Link href="/backtests">Backtests</Link>
          </nav>
        </header>
        <main>{children}</main>
        <footer className="foot">
          Decision support and PAPER simulation only. No profit is implied; nothing here is
          investment advice.
        </footer>
      </body>
    </html>
  );
}
