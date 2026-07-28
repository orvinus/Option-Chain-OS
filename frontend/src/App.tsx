import { useState } from "react";
import { ChartsPage } from "./pages/ChartsPage";
import { Dashboard } from "./pages/Dashboard";
import { MultiTimeframePage } from "./pages/MultiTimeframePage";
import { RatioChartPage } from "./pages/RatioChartPage";
import { ReplayPage } from "./pages/ReplayPage";
import { SymbolChangeConfirm } from "./components/SymbolChangeConfirm";
import { useMarketContext } from "./hooks/useMarketContext";

type Page = "oi-change" | "charts" | "mtf" | "ratio" | "replay";

const TABS: { id: Page; label: string }[] = [
  { id: "oi-change", label: "OI Change" },
  { id: "charts", label: "Charts" },
  { id: "mtf", label: "Multi-TF" },
  { id: "ratio", label: "Ratio" },
  { id: "replay", label: "Replay" },
];

export default function App() {
  // One shared market context (auth/health poll, symbol, expiry, ATM window) so
  // the selection — and a single health poll — persists across tab switches.
  const mc = useMarketContext();
  const [page, setPage] = useState<Page>("oi-change");

  return (
    <>
      {mc.authenticated && (
        <nav className="w-full max-w-[1500px] mx-auto px-4 md:px-6 pt-3">
          <div className="flex items-center gap-2">
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
          </div>
        </nav>
      )}
      {page === "charts" ? (
        <ChartsPage mc={mc} />
      ) : page === "mtf" ? (
        <MultiTimeframePage mc={mc} />
      ) : page === "ratio" ? (
        <RatioChartPage mc={mc} />
      ) : page === "replay" ? (
        <ReplayPage mc={mc} />
      ) : (
        <Dashboard mc={mc} />
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
