/**
 * Sub-tab — Engines: the three entry-filter engine dashboards, bound to one
 * zone's configuration at a time via the day/zone context selector. The MTF
 * Ratio and Quant Action views land with their milestones.
 */
import { useEffect, useState } from "react";
import type { AlgoConfigDoc, Weekday, ZoneId } from "../../../types/algo";
import { WEEKDAY_LABEL, WEEKDAYS, ZONE_IDS } from "../../../types/algo";
import { SelectField } from "../controls";
import { CopySettingsDialog } from "../CopySettingsDialog";
import { MqaePanel } from "./MqaePanel";
import { MtfRatioPanel } from "./MtfRatioPanel";
import { OiStructurePanel } from "./OiStructurePanel";
import { UmpPanel } from "./UmpPanel";
import { trackRequest } from "../../../api/inflight";

export type EngineKey = "oi_structure" | "mtf_ratio" | "mqae" | "ump";

/** Dashboard nav entries — labels match the spec's indicator slot names. */
export const ENGINE_TABS: { id: EngineKey; label: string }[] = [
  { id: "oi_structure", label: "OI Change" },
  { id: "mtf_ratio", label: "Multi-TF" },
  { id: "mqae", label: "Ratio" },
  { id: "ump", label: "Ultra Master Pro" },
];

const ENGINE_TITLE: Record<EngineKey, string> = {
  oi_structure: "OI Structure Engine — the “OI Change” entry filter",
  mtf_ratio: "MTF Ratio — the “Multi-TF” entry filter",
  mqae: "Master Quantitative Action Engine — the “Ratio” entry filter",
  ump: "NIFTY Ultra Master Pro — execution engine (entries + all exits)",
};

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  dirty: boolean;
  engine: EngineKey;
  day: Weekday;
  zone: ZoneId;
  expiry: string;                       // "" = current weekly (auto)
  histDate: string;                     // "" = live (page-owned so backtest deep-links can set it)
  at: string;                           // "" = end of session; "HH:MM" freezes the eval
  configVersion: number | null;         // pin the eval to a saved config version
  configRun?: number | null;            // pin to a backtest run's frozen config
  configSandbox?: boolean;              // evaluate under the sandbox document
  livePoll?: boolean;                   // false in the Backtesting workspace
  onDayChange: (d: Weekday) => void;
  onZoneChange: (z: ZoneId) => void;
  onExpiryChange: (e: string) => void;
  onHistDateChange: (d: string) => void;
  onAtChange: (t: string) => void;
  onConfigVersionChange: (v: number | null) => void;
  defaults: AlgoConfigDoc | null;       // Appendix-A reference (reset source)
  onRequestSave: () => void;            // the page's save flow (confirm-diff)
  /** Deep-link contract for the UMP panel (a replay trade's own strike/side). */
  initialStrike?: number | null;
  initialOptionType?: "CE" | "PE" | null;
}

const WD_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "2026-08-18" → "2026-08-18 · Tue" (label for expiry dropdowns). */
export function expiryLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso : `${iso} · ${WD_SHORT[d.getDay()]}`;
}

export function EnginesPanel({
  draft, mutate, dirty, engine, day, zone, expiry, histDate, at, configVersion,
  configRun, configSandbox, livePoll = true,
  onDayChange, onZoneChange, onExpiryChange, onHistDateChange, onAtChange,
  onConfigVersionChange, defaults, onRequestSave, initialStrike, initialOptionType,
}: Props) {
  const [expiries, setExpiries] = useState<string[]>([]);
  // §9 copy dialog, seeded with the active engine × day × zone as source.
  const [copyOpen, setCopyOpen] = useState(false);
  const onCopyTo = () => setCopyOpen(true);
  const setDay = onDayChange;
  const setZone = onZoneChange;
  const setHistDate = onHistDateChange;

  // The evaluated symbol is the selected DAY's index (SENSEX days list SENSEX
  // expiries — this dropdown was hardcoded to NIFTY until 2026-08-18).
  const daySymbol = draft.days[day]?.index_symbol ?? "NIFTY";

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        // Wrapped so this raw fetch is counted like every algoApi call and
        // the app-wide loading indicator can see it.
        const res = await trackRequest(() =>
          fetch(`/api/expiries?symbol=${encodeURIComponent(daySymbol)}`, {
            credentials: "same-origin",
          }),
        );
        if (res.ok && !cancelled) {
          const body = (await res.json()) as { expiries: string[] };
          setExpiries(body.expiries ?? []);
        }
      } catch {
        /* selector falls back to "current (auto)" only */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [daySymbol]);

  // A pinned expiry from another symbol's chain is meaningless — clear it
  // when the day's index changes under the selection.
  useEffect(() => {
    if (expiry && expiries.length > 0 && !expiries.includes(expiry)) {
      onExpiryChange("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [daySymbol, expiries]);

  return (
    <div className="flex flex-col gap-4">
      <div className="panel px-4 py-3 flex items-center gap-3 flex-wrap">
        <span className="text-[12.5px] font-bold text-gray-100">
          {ENGINE_TITLE[engine]}
        </span>
        <div className="flex-1" />
        <span className="text-[10px] text-muted">Zone context</span>
        <div className="w-32">
          <SelectField
            value={day}
            options={WEEKDAYS.map((w) => ({ value: w, label: WEEKDAY_LABEL[w] }))}
            onChange={setDay}
          />
        </div>
        <div className="w-20">
          <SelectField
            value={zone}
            options={ZONE_IDS.map((z) => ({ value: z, label: z }))}
            onChange={setZone}
          />
        </div>
        <span
          className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-bold"
          title="The index this day trades (set per day in Daily Trading Config)"
        >
          {daySymbol}
        </span>
        <span className="text-[10px] text-muted">Expiry</span>
        <div className="w-44">
          <SelectField
            value={expiry}
            options={[
              { value: "", label: "current (auto)" },
              ...expiries.map((e) => ({ value: e, label: expiryLabel(e) })),
            ]}
            onChange={onExpiryChange}
          />
        </div>
        <input
          type="date"
          title="As-of session (blank = live). Ultra Master Pro always replays the contract's FULL stored life up to this day — the TradingView chart; the other engines evaluate this session."
          className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-accent"
          value={histDate}
          onChange={(e) => setHistDate(e.target.value)}
        />
        {histDate && (
          <>
            <span className="text-[10px] text-muted">At</span>
            <input
              type="time"
              title="Replay cursor: stop the evaluation at this IST minute (blank = end of day)"
              className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-accent"
              value={at}
              onChange={(e) => onAtChange(e.target.value)}
            />
            <button
              type="button"
              className="pill text-xs"
              onClick={() => {
                setHistDate("");
                onAtChange("");
              }}
            >
              Live
            </button>
          </>
        )}
      </div>

      {(configVersion != null || configRun != null) && (
        <div className="panel px-4 py-2 flex items-center gap-3 flex-wrap border border-amber-500/40">
          <span className="text-[11px] text-amber-300 font-bold">
            ⚙ Evaluating under{" "}
            {configRun != null
              ? `backtest run #${configRun}'s frozen config`
              : `pinned config v${configVersion}`}
          </span>
          <span className="text-[10.5px] text-muted">
            (the editors below still edit the {configSandbox ? "SANDBOX" : "LIVE"} draft)
          </span>
          <div className="flex-1" />
          <button
            type="button"
            className="pill text-xs"
            onClick={() => onConfigVersionChange(null)}
          >
            Clear pin{configSandbox ? " — use sandbox config" : " — use live config"}
          </button>
        </div>
      )}

      {engine === "oi_structure" && (
        <OiStructurePanel
          draft={draft}
          mutate={mutate}
          day={day}
          zone={zone}
          dirty={dirty}
          histDate={histDate}
          expiry={expiry}
          at={at}
          configVersion={configVersion}
          configRun={configRun}
          configSandbox={configSandbox}
          livePoll={livePoll}
          defaults={defaults}
          onRequestSave={onRequestSave}
          onCopyTo={onCopyTo}
        />
      )}
      {engine === "mtf_ratio" && (
        <MtfRatioPanel
          draft={draft}
          mutate={mutate}
          day={day}
          zone={zone}
          dirty={dirty}
          histDate={histDate}
          expiry={expiry}
          at={at}
          configVersion={configVersion}
          configRun={configRun}
          configSandbox={configSandbox}
          livePoll={livePoll}
          defaults={defaults}
          onRequestSave={onRequestSave}
          onCopyTo={onCopyTo}
        />
      )}
      {engine === "mqae" && (
        <MqaePanel
          draft={draft}
          mutate={mutate}
          day={day}
          zone={zone}
          dirty={dirty}
          histDate={histDate}
          expiry={expiry}
          at={at}
          configVersion={configVersion}
          configRun={configRun}
          configSandbox={configSandbox}
          livePoll={livePoll}
          defaults={defaults}
          onRequestSave={onRequestSave}
          onCopyTo={onCopyTo}
        />
      )}
      {engine === "ump" && (
        <UmpPanel
          draft={draft}
          mutate={mutate}
          day={day}
          zone={zone}
          dirty={dirty}
          histDate={histDate}
          expiry={expiry}
          at={at}
          configVersion={configVersion}
          configRun={configRun}
          configSandbox={configSandbox}
          livePoll={livePoll}
          defaults={defaults}
          onRequestSave={onRequestSave}
          initialStrike={initialStrike}
          initialOptionType={initialOptionType}
          onCopyTo={onCopyTo}
        />
      )}
      {copyOpen && (
        <CopySettingsDialog
          draft={draft}
          mutate={mutate}
          initialScope={{ kind: "indicator", day, zone, engine }}
          onClose={() => setCopyOpen(false)}
        />
      )}
    </div>
  );
}
