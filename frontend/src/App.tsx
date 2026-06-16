import { useState } from "react";
import { ChartsPage } from "./pages/ChartsPage";
import { Dashboard } from "./pages/Dashboard";
import { useMarketContext } from "./hooks/useMarketContext";

type Page = "oi-change" | "charts";

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
            <button
              type="button"
              onClick={() => setPage("oi-change")}
              className={`pill ${page === "oi-change" ? "pill-active" : ""}`}
            >
              OI Change
            </button>
            <button
              type="button"
              onClick={() => setPage("charts")}
              className={`pill ${page === "charts" ? "pill-active" : ""}`}
            >
              Charts
            </button>
          </div>
        </nav>
      )}
      {page === "charts" ? <ChartsPage mc={mc} /> : <Dashboard mc={mc} />}
    </>
  );
}
