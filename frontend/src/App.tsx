import { useState } from "react";
import type { ComponentType } from "react";
import { AlgoConfigPage } from "./pages/AlgoConfigPage";
import { BacktestingPage } from "./pages/BacktestingPage";
import { ChartsPage } from "./pages/ChartsPage";
import { Dashboard } from "./pages/Dashboard";
import { MultiTimeframePage } from "./pages/MultiTimeframePage";
import { RatioChartPage } from "./pages/RatioChartPage";
import { ReplayPage } from "./pages/ReplayPage";
import { SymbolChangeConfirm } from "./components/SymbolChangeConfirm";
import { useMarketContext } from "./hooks/useMarketContext";
import type { MarketContextValue } from "./hooks/useMarketContext";
import { GlobalLoadingBar } from "./components/Loading";

type Page =
  | "oi-change" | "charts" | "mtf" | "ratio" | "replay"
  | "algo-config" | "backtesting";

const TABS: { id: Page; label: string }[] = [
  { id: "oi-change", label: "OI Change" },
  { id: "charts", label: "Charts" },
  { id: "mtf", label: "Multi-TF" },
  { id: "ratio", label: "Ratio" },
  { id: "replay", label: "Replay" },
  { id: "algo-config", label: "Algo Config" },
  { id: "backtesting", label: "Backtesting" },
];

const PAGES: Record<Page, ComponentType<{ mc: MarketContextValue }>> = {
  "oi-change": Dashboard,
  charts: ChartsPage,
  mtf: MultiTimeframePage,
  ratio: RatioChartPage,
  replay: ReplayPage,
  "algo-config": AlgoConfigPage,
  backtesting: BacktestingPage,
};

export default function App() {
  // One shared market context (auth/health poll, symbol, expiry, ATM window) so
  // the selection — and a single health poll — persists across tab switches.
  const mc = useMarketContext();
  const [page, setPage] = useState<Page>("oi-change");
  const Active = PAGES[page];

  return (
    <>
      {/* App-wide loading indicator. Mounted once, driven by the in-flight
          counter in api/inflight.ts, so every request on every page shows
          "something is loading" without each panel wiring it up. */}
      <GlobalLoadingBar />
      {/* Tabs available whenever the BACKEND is up — a broker outage must not hide
          the historical views behind a "connecting" screen. */}
      {mc.dataReady && (
        <nav className="w-full max-w-[1500px] mx-auto px-4 md:px-6 pt-3">
          <div className="flex items-center gap-2 flex-wrap">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setPage(t.id)}
                className={`pill ${page === t.id ? "pill-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
            <div className="flex-1" />
            <a
              href="/platform-guide.html"
              target="_blank"
              rel="noreferrer"
              className="pill text-xs"
              title="Ultra-detailed guide to every page and button"
            >
              📖 Guide
            </a>
          </div>
        </nav>
      )}
      <Active mc={mc} />
      {/* One shared confirmation gate for leaving the NIFTY 50 default (all tabs). */}
      <SymbolChangeConfirm
        pendingSymbol={mc.pendingSymbol}
        onConfirm={() => void mc.confirmSymbolChange()}
        onCancel={mc.cancelSymbolChange}
      />
    </>
  );
}
