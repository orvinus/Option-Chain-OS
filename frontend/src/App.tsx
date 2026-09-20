import { useEffect, useState } from "react";
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
import { algoApi } from "./api/algoRest";
import { isViewerMirror } from "./api/viewerMode";

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

/** Tabs a read-only viewer (the /seceretdashboard session) may open.
 *  Everything else is market-data tooling they were not given access to. */
const VIEWER_TABS: Page[] = ["algo-config", "backtesting"];

export default function App() {
  // One shared market context (auth/health poll, symbol, expiry, ATM window) so
  // the selection — and a single health poll — persists across tab switches.
  const mc = useMarketContext();
  const [viewer, setViewer] = useState(false);
  const [page, setPage] = useState<Page>("oi-change");

  // Ask the SERVER for the role — a query flag would let anyone narrow (or
  // widen) their own view by editing the URL.
  useEffect(() => {
    // Same rule as the gate: the narrowed tab set belongs to the mirror entry
    // point, not to the session. "/" is left exactly as it was for everyone.
    if (!isViewerMirror()) return;
    let alive = true;
    algoApi
      .me()
      .then((id) => {
        if (!alive || id?.role !== "viewer") return;
        setViewer(true);
        setPage("algo-config");
      })
      .catch(() => {
        /* no session — normal operator, full tab set */
      });
    return () => {
      alive = false;
    };
  }, []);

  const tabs = viewer ? TABS.filter((t) => VIEWER_TABS.includes(t.id)) : TABS;
  // Belt and braces: even if `page` were set some other way, a viewer can only
  // ever render one of their own pages.
  const safePage = viewer && !VIEWER_TABS.includes(page) ? "algo-config" : page;
  const Active = PAGES[safePage];

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
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setPage(t.id)}
                className={`pill ${safePage === t.id ? "pill-active" : ""}`}
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
            <a
              href="/algo-deep-guide.html"
              target="_blank"
              rel="noreferrer"
              className="pill text-xs"
              title="Ultra-deep guide to every control in Algo Config and Backtesting"
            >
              🧭 Algo &amp; Backtest Guide
            </a>
          </div>
        </nav>
      )}
      {viewer ? (
        <>
          <div className="w-full max-w-[1500px] mx-auto px-4 md:px-6 pt-2">
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-200">
              <b>View only.</b> Browse Algo Config and Backtesting freely —
              every page, panel and setting. Saving, resetting, copying and
              starting runs are blocked, so nothing here can change the platform.
            </div>
          </div>
          {/* NOT inert: a viewer must be able to browse — switch day/zone,
              open sub-tabs, expand panels, pick a backtest run, scroll. The
              real protection is the API guard, which refuses every non-GET
              before it leaves the browser, plus the server's 403. Config edits
              are draft-only until Save, so touching a field changes nothing;
              the Save itself is what cannot happen. */}
          <div className="viewer-readonly">
            <Active mc={mc} />
          </div>
        </>
      ) : (
        <Active mc={mc} />
      )}
      {/* One shared confirmation gate for leaving the NIFTY 50 default (all tabs). */}
      <SymbolChangeConfirm
        pendingSymbol={mc.pendingSymbol}
        onConfirm={() => void mc.confirmSymbolChange()}
        onCancel={mc.cancelSymbolChange}
      />
    </>
  );
}
