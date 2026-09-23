/**
 * Algo Config — the trading engine's control room.
 *
 * Structure (reference: algo_config_full.html): a real admin gate (server-side
 * session; every /api/algo/* call 401s without it), then a sticky sub-tab bar
 * with the §13 save workflow — edits accumulate in a local draft, Save shows
 * the exact field diff, and only Confirm posts the document as one atomic new
 * version.
 *
 * Sub-tabs land with their milestones: Daily Trading Config, Validation,
 * Security/Audit, Holidays, Integrations and Paper settings are live now; the
 * engine dashboards (OI Structure / MTF Ratio / Quant Action / Ultra Master
 * Pro) and the P&L ledgers arrive with their engines.
 */
import { useMemo, useState } from "react";
import { algoApi } from "../api/algoRest";
import type { MarketContextValue } from "../hooks/useMarketContext";
import type { AlgoConfigDoc, Weekday } from "../types/algo";
import { killExitNote } from "../types/algo";
import { AlgoAdminGate } from "../components/algo/AlgoAdminGate";
import { ConfirmSaveModal } from "../components/algo/ConfirmSaveModal";
import { DailyConfigPanel } from "../components/algo/DailyConfigPanel";
import { ENGINE_TABS, EnginesPanel } from "../components/algo/engines/EnginesPanel";
import type { EngineKey } from "../components/algo/engines/EnginesPanel";
import type { IndicatorKey, ZoneId } from "../types/algo";
import {
  HolidaysPanel,
  IntegrationsPanel,
  PaperPanel,
} from "../components/algo/MiscPanels";
import { PnlPanel } from "../components/algo/PnlPanel";
import { useConfigDraft } from "../hooks/useConfigDraft";
import { AlgoStreamContext, useAlgoStream } from "../hooks/useAlgoStream";
import { LiveStrip } from "../components/algo/LiveStrip";
import { SecurityPanel } from "../components/algo/SecurityPanel";
import { ValidationPanel } from "../components/algo/ValidationPanel";
import type { AlgoIdentity } from "../types/algo";

type SubTab =
  | "daily"
  | "engines"
  | "pnl"
  | "paper"
  | "holidays"
  | "integrations"
  | "security"
  | "validation";

// The four engine dashboards render inside the "engines" tab; each gets its
// OWN nav pill (spec: dashboards reachable from the Algo Config nav directly).
const SUB_TABS_BEFORE: { id: SubTab; label: string }[] = [
  { id: "daily", label: "Daily Trading Config" },
];
const SUB_TABS_AFTER: { id: SubTab; label: string }[] = [
  { id: "pnl", label: "P&L Summary" },
  { id: "paper", label: "Paper Trading" },
  { id: "holidays", label: "Holiday Calendar" },
  { id: "integrations", label: "Integrations & Broker" },
  { id: "security", label: "Security & Backups" },
  { id: "validation", label: "Validation Summary" },
];

/** Entry-filter indicator slot → the engine dashboard that powers it. */
const IND_TO_ENGINE: Record<IndicatorKey, EngineKey> = {
  oi_change: "oi_structure",
  multi_tf: "mtf_ratio",
  ratio: "mqae",
};

async function loadLiveConfig() {
  const env = await algoApi.getConfig();
  return { config: env.config, meta: { version: env.version } };
}

async function saveLiveConfig(
  config: AlgoConfigDoc, note: string, meta: { version: number } | null,
): Promise<string> {
  const res = await algoApi.saveConfig(config, note, meta?.version ?? null);
  return (
    `✔ Saved as v${res.version}` +
    (res.warnings.length > 0 ? ` with ${res.warnings.length} warning(s)` : "") +
    killExitNote(res)
  );
}

function todayIstWeekday(): Weekday | null {
  const name = new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    timeZone: "Asia/Kolkata",
  })
    .format(new Date())
    .toLowerCase();
  return (["monday", "tuesday", "wednesday", "thursday", "friday"] as Weekday[]).find(
    (w) => w === name
  ) ?? null;
}

export function AlgoConfigPage({ mc }: { mc: MarketContextValue }) {
  return (
    <AlgoAdminGate>
      {(identity, signOut) => <AlgoConfigInner mc={mc} identity={identity} signOut={signOut} />}
    </AlgoAdminGate>
  );
}

function AlgoConfigInner({
  mc,
  identity,
  signOut,
}: {
  mc: MarketContextValue;
  identity: AlgoIdentity;
  signOut: () => void;
}) {
  const [tab, setTab] = useState<SubTab>("daily");
  const [engine, setEngine] = useState<EngineKey>("oi_structure");
  const [engDay, setEngDay] = useState<Weekday>(() => todayIstWeekday() ?? "monday");
  const [engZone, setEngZone] = useState<ZoneId>("Z1");
  const [engExpiry, setEngExpiry] = useState("");   // "" = current weekly (auto)
  const [engHistDate, setEngHistDate] = useState("");   // "" = live session
  const [engAt, setEngAt] = useState("");               // "" = whole session
  const [engCfgVer, setEngCfgVer] = useState<number | null>(null);  // config pin
  const {
    meta, draft, loadError, reload, mutate, dirty, changes, doSave, requestSave,
    discard, showConfirm, setShowConfirm, saveBusy, saveErrors, toast,
    refreshKey, setRefreshKey, defaults,
  } = useConfigDraft(loadLiveConfig, saveLiveConfig);

  const todayWd = useMemo(() => todayIstWeekday(), []);

  // One socket for the whole page, held above the tab switch so a sub-tab
  // change never re-handshakes. Frozen while the user has pinned a historical
  // date or an `At` minute — a pinned cursor must stay pinned.
  // The Ultra Master Pro chart pins the stream to the contract it is showing,
  // so the live forming candle is always for THAT contract. The stream used to
  // be fixed to CE + its own band pick: a PE chart never got a live candle at
  // all, and a CE chart lost it whenever the two strike picks disagreed — the
  // chart then sat still until the next 5-minute candle (reported 2026-09-23).
  const [chartPin, setChartPin] = useState<{ strike: number; optionType: "CE" | "PE" } | null>(null);
  const streamScope = useMemo(
    () =>
      draft
        ? {
            day: engDay,
            zone: engZone,
            symbol: draft.days[engDay]?.index_symbol ?? null,
            expiry: engExpiry || null,
            strike: chartPin?.strike ?? null,
            optionType: chartPin?.optionType ?? "CE",
          }
        : null,
    [draft, engDay, engZone, engExpiry, chartPin],
  );
  const streamRaw = useAlgoStream(streamScope, !engHistDate && !engAt);
  const stream = useMemo(() => ({ ...streamRaw, pinContract: setChartPin }), [streamRaw]);

  const liveVersion = meta?.version;
  const paperOn = draft?.global.paper.paper_mode ?? false;
  const masterKilled = draft?.global.master_kill ?? false;

  if (loadError) {
    return (
      <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-6">
        <div className="text-sm text-ce">{loadError}</div>
      </div>
    );
  }
  if (!draft) {
    return (
      <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-6">
        <div className="text-sm text-muted">Loading configuration…</div>
      </div>
    );
  }

  return (
    // One stream for the whole page. Deep children (the UMP chart) read the
    // newest frame from here instead of it being drilled through EnginesPanel,
    // and it survives sub-tab switches that unmount the panels.
    <AlgoStreamContext.Provider value={stream}>
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      {/* Sticky control bar — sub-tabs + status + save workflow. Same opaque
          wrapper trick as ReplayPage: nothing between this and the viewport
          may set overflow. */}
      <div className="sticky top-0 z-30 -mx-4 md:-mx-6 -mt-3 px-4 md:px-6 pt-3 pb-3 bg-bg">
        <div className="panel px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-2 flex-wrap">
            {SUB_TABS_BEFORE.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`pill ${tab === t.id ? "pill-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
            {ENGINE_TABS.map((e) => (
              <button
                key={e.id}
                type="button"
                onClick={() => {
                  setEngine(e.id);
                  setTab("engines");
                }}
                className={`pill ${tab === "engines" && engine === e.id ? "pill-active" : ""}`}
              >
                {e.label}
              </button>
            ))}
            {SUB_TABS_AFTER.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`pill ${tab === t.id ? "pill-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
            <div className="flex-1" />
            <span className="text-[11px] text-muted">
              v{liveVersion ?? "…"} · {identity.username}
            </span>
            <button
              type="button"
              className="pill text-xs"
              title="Change the Algo Config / Backtesting sign-in ID and password"
              onClick={() => {
                setTab("security");
                // The tab renders on the next frame; scroll once it exists.
                window.setTimeout(() => {
                  document
                    .getElementById("change-credentials")
                    ?.scrollIntoView({ behavior: "smooth", block: "center" });
                }, 80);
              }}
            >
              🔑 Change password
            </button>
            <button type="button" className="pill text-xs" onClick={signOut}>
              Sign out
            </button>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            {masterKilled && (
              <span className="text-[11px] font-bold text-ce">■ MASTER KILL ACTIVE</span>
            )}
            {paperOn && (
              <span className="text-[11px] font-bold text-amber-400">■ PAPER MODE</span>
            )}
            <span className="text-[11px] text-muted">
              {mc.symbolDisplay ?? mc.symbol}
              {mc.liveSpot != null ? ` · ${mc.liveSpot.toFixed(2)}` : ""}
            </span>
            <LiveStrip
              frame={stream.frame}
              status={stream.status}
              ageS={stream.ageS}
              error={stream.error}
            />
            <div className="flex-1" />
            {dirty ? (
              <>
                <span className="text-[11px] text-amber-300">
                  {changes.length} unsaved change{changes.length === 1 ? "" : "s"}
                </span>
                <button type="button" className="pill text-xs" onClick={discard}>
                  Discard
                </button>
                <button
                  type="button"
                  className="pill pill-active text-xs"
                  onClick={requestSave}
                >
                  Save…
                </button>
              </>
            ) : (
              <span className="text-[11px] text-muted">All changes saved</span>
            )}
          </div>
        </div>
      </div>

      <div className="pt-4">
        {tab === "daily" && (
          <DailyConfigPanel
            draft={draft}
            mutate={mutate}
            todayWeekday={todayWd}
            defaults={defaults}
            onOpenEngine={(ind, day, zone) => {
              setEngine(ind === "ump" ? "ump" : IND_TO_ENGINE[ind]);
              setEngDay(day);
              setEngZone(zone);
              setTab("engines");
            }}
          />
        )}
        {tab === "engines" && (
          <EnginesPanel
            draft={draft}
            mutate={mutate}
            dirty={dirty}
            engine={engine}
            day={engDay}
            zone={engZone}
            expiry={engExpiry}
            histDate={engHistDate}
            at={engAt}
            configVersion={engCfgVer}
            onDayChange={setEngDay}
            onZoneChange={setEngZone}
            onExpiryChange={setEngExpiry}
            onHistDateChange={setEngHistDate}
            onAtChange={setEngAt}
            onConfigVersionChange={setEngCfgVer}
            defaults={defaults}
            onRequestSave={requestSave}
          />
        )}
        {tab === "pnl" && <PnlPanel />}
        {tab === "paper" && (
          <PaperPanel draft={draft} mutate={mutate} onAfterReset={() => void reload()} />
        )}
        {tab === "holidays" && <HolidaysPanel draft={draft} mutate={mutate} />}
        {tab === "integrations" && <IntegrationsPanel draft={draft} mutate={mutate} />}
        {tab === "security" && (
          <SecurityPanel
            identity={identity}
            refreshKey={refreshKey}
            draft={draft}
            mutate={mutate}
            onRestored={() => {
              void reload();
              setRefreshKey((k) => k + 1);
            }}
          />
        )}
        {tab === "validation" && <ValidationPanel refreshKey={refreshKey} />}
      </div>

      {showConfirm && (
        <ConfirmSaveModal
          changes={changes}
          busy={saveBusy}
          errors={saveErrors}
          onConfirm={(note) => void doSave(note)}
          onCancel={() => setShowConfirm(false)}
        />
      )}

      {toast && (
        <div className="fixed bottom-5 right-5 z-50 bg-pe/10 border border-pe/40 text-pe text-sm font-semibold px-4 py-2.5 rounded-lg shadow-lg">
          {toast}
        </div>
      )}
    </div>
    </AlgoStreamContext.Provider>
  );
}
