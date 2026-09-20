/**
 * Per-run scenario overrides — a pure transform applied to a base config
 * document at run-creation time (submitted as source:"inline", so the run
 * freezes exactly what you asked for; the sandbox/live docs are untouched).
 *
 * Every knob applies to ALL five days / fifteen zones — these are the
 * high-impact levers that decide HOW OFTEN the engine can enter. Fine-граin
 * per-zone editing stays in the workspace tabs.
 */
import { cloneDoc } from "../algoDraft";
import type { AlgoConfigDoc, IndicatorKey } from "../../../types/algo";

export interface RunOverrides {
  /** null = keep as configured. */
  indicators: IndicatorKey[] | null;
  cadence: "zone_start" | "every_candle" | null;
  directionHoldMin: number | null;
  premiumMin: number | null;
  premiumMax: number | null;
  maxTrades: number | null;
  /** Top-N strikes hunted in parallel (1 = original single-strike). */
  strikeScanCount: number | null;
  /** true = replace each day's zones with ONE wide zone 09:20–15:10. */
  singleWideZone: boolean;
  allocationPct: number | null;       // 101 = ALL-IN
  clearDayKills: boolean;
  umpEnableLong: boolean | null;
  umpEnableRetest: boolean | null;
  /** UMP engine numerics (Pine inputs the engine actually consumes) —
   *  null = keep each zone's configured value. */
  umpExpansionLawPct: number | null;
  umpBridgeLawPct: number | null;
  umpMedianLawPct: number | null;
  umpPriceFloorInr: number | null;
  umpBodyMatchPts: number | null;
  umpScanDepthBars: number | null;
  umpZoneWidthPct: number | null;
  umpMaxSlPct: number | null;
  umpTriggerTimeoutBars: number | null;
  /** UMP entry candle timeframe for every zone (§1); null = keep. */
  umpEntryTimeframeMin: 5 | 15 | null;
  /** UMP entry kinds switched OFF in every zone (§2); null = keep. */
  umpScenariosOff: string[] | null;
}

export const EMPTY_OVERRIDES: RunOverrides = {
  indicators: null,
  cadence: null,
  directionHoldMin: null,
  premiumMin: null,
  premiumMax: null,
  maxTrades: null,
  strikeScanCount: null,
  singleWideZone: false,
  allocationPct: null,
  clearDayKills: false,
  umpEnableLong: null,
  umpEnableRetest: null,
  umpExpansionLawPct: null,
  umpBridgeLawPct: null,
  umpMedianLawPct: null,
  umpPriceFloorInr: null,
  umpBodyMatchPts: null,
  umpScanDepthBars: null,
  umpZoneWidthPct: null,
  umpMaxSlPct: null,
  umpTriggerTimeoutBars: null,
  umpEntryTimeframeMin: null,
  umpScenariosOff: null,
};

export function overridesActive(o: RunOverrides): boolean {
  return JSON.stringify(o) !== JSON.stringify(EMPTY_OVERRIDES);
}

/** Human strings for run provenance (stored in settings.overrides). */
export function describeOverrides(o: RunOverrides): string[] {
  const out: string[] = [];
  if (o.indicators) out.push(`indicators: ${o.indicators.join("+") || "none"}`);
  if (o.cadence) out.push(`cadence: ${o.cadence}`);
  if (o.directionHoldMin != null) out.push(`direction-hold: ${o.directionHoldMin}m`);
  if (o.premiumMin != null || o.premiumMax != null)
    out.push(`band: ${o.premiumMin ?? "keep"}–${o.premiumMax ?? "keep"}`);
  if (o.maxTrades != null) out.push(`max trades/zone: ${o.maxTrades}`);
  if (o.strikeScanCount != null) out.push(`strike scan: top ${o.strikeScanCount}`);
  if (o.singleWideZone) out.push("single wide zone 09:20–15:10");
  if (o.allocationPct != null)
    out.push(o.allocationPct > 100 ? "ALL-IN" : `allocation ${o.allocationPct}%`);
  if (o.clearDayKills) out.push("day kills cleared");
  if (o.umpEnableLong != null) out.push(`UMP long: ${o.umpEnableLong ? "on" : "off"}`);
  if (o.umpEnableRetest != null) out.push(`UMP retest: ${o.umpEnableRetest ? "on" : "off"}`);
  if (o.umpExpansionLawPct != null) out.push(`UMP expansion law: ${o.umpExpansionLawPct}%`);
  if (o.umpBridgeLawPct != null) out.push(`UMP bridge law: ${o.umpBridgeLawPct}%`);
  if (o.umpMedianLawPct != null) out.push(`UMP median law: ${o.umpMedianLawPct}%`);
  if (o.umpPriceFloorInr != null) out.push(`UMP price floor: ₹${o.umpPriceFloorInr}`);
  if (o.umpBodyMatchPts != null) out.push(`UMP body match: ${o.umpBodyMatchPts} pts`);
  if (o.umpScanDepthBars != null) out.push(`UMP scan depth: ${o.umpScanDepthBars} bars`);
  if (o.umpZoneWidthPct != null) out.push(`UMP zone width: ${o.umpZoneWidthPct}%`);
  if (o.umpMaxSlPct != null) out.push(`UMP max SL: ${o.umpMaxSlPct}%`);
  if (o.umpTriggerTimeoutBars != null) out.push(`UMP trigger timeout: ${o.umpTriggerTimeoutBars} bars`);
  if (o.umpEntryTimeframeMin != null) out.push(`UMP entry TF: ${o.umpEntryTimeframeMin}m`);
  if (o.umpScenariosOff && o.umpScenariosOff.length) out.push(`UMP off: ${o.umpScenariosOff.join(",")}`);
  return out;
}

export function applyOverrides(base: AlgoConfigDoc, o: RunOverrides): AlgoConfigDoc {
  const doc = cloneDoc(base);
  for (const day of Object.values(doc.days)) {
    if (o.clearDayKills) day.day_kill = false;
    if (o.allocationPct != null) {
      if (o.allocationPct > 100) {
        day.all_in = true;
      } else {
        day.all_in = false;
        day.allocation_pct = o.allocationPct;
      }
    }
    const zoneIds = Object.keys(day.zones) as (keyof typeof day.zones)[];
    if (o.singleWideZone && zoneIds.length > 0) {
      // First zone covers the whole session; the rest park (killed) at the
      // tail so zone-count invariants and §15 windows stay valid.
      const tail: [string, string][] = [["15:10", "15:12"], ["15:12", "15:14"], ["15:14", "15:16"]];
      zoneIds.forEach((zid, i) => {
        const z = day.zones[zid];
        if (i === 0) {
          z.start = "09:20";
          z.end = "15:10";
          z.zone_kill = false;
        } else {
          const t = tail[Math.min(i - 1, tail.length - 1)];
          z.start = t[0];
          z.end = t[1];
          z.zone_kill = true;
        }
      });
    }
    for (const z of Object.values(day.zones)) {
      if (o.indicators) z.enabled_indicators = [...o.indicators];
      if (o.cadence) z.reeval_cadence = o.cadence;
      if (o.directionHoldMin != null) z.direction_hold_min = o.directionHoldMin;
      if (o.premiumMin != null) z.premium_min = o.premiumMin;
      if (o.premiumMax != null) z.premium_max = o.premiumMax;
      if (o.maxTrades != null) z.max_trades = o.maxTrades;
      if (o.strikeScanCount != null) z.strike_scan_count = o.strikeScanCount;
      if (o.umpEnableLong != null) z.ump.entry.enable_long = o.umpEnableLong;
      if (o.umpEnableRetest != null) z.ump.entry.enable_retest = o.umpEnableRetest;
      if (o.umpExpansionLawPct != null) z.ump.institutional.expansion_law_pct = o.umpExpansionLawPct;
      if (o.umpBridgeLawPct != null) z.ump.institutional.bridge_law_pct = o.umpBridgeLawPct;
      if (o.umpMedianLawPct != null) z.ump.institutional.median_law_pct = o.umpMedianLawPct;
      if (o.umpPriceFloorInr != null) z.ump.institutional.price_floor_inr = o.umpPriceFloorInr;
      if (o.umpBodyMatchPts != null) z.ump.structural.body_match_pts = o.umpBodyMatchPts;
      if (o.umpScanDepthBars != null) z.ump.structural.scan_depth_bars = o.umpScanDepthBars;
      if (o.umpZoneWidthPct != null) z.ump.visual.zone_width_pct = o.umpZoneWidthPct;
      if (o.umpMaxSlPct != null) z.ump.entry.max_sl_pct = o.umpMaxSlPct;
      if (o.umpTriggerTimeoutBars != null) z.ump.entry.trigger_timeout_bars = o.umpTriggerTimeoutBars;
      if (o.umpEntryTimeframeMin != null) z.ump.entry.entry_timeframe_min = o.umpEntryTimeframeMin;
      if (o.umpScenariosOff) {
        const sc = { ...(z.ump.entry.scenarios ?? {}) };
        for (const k of o.umpScenariosOff) sc[k] = false;
        z.ump.entry.scenarios = sc;
      }
      if (o.singleWideZone || o.clearDayKills) z.strategy_active = true;
    }
  }
  return doc;
}

/** The scenario suite — each preset is just an overrides object. */
export const PRESETS: { key: string; label: string; desc: string; o: RunOverrides }[] = [
  {
    key: "baseline", label: "Baseline (as configured)",
    desc: "no overrides — exactly the chosen config",
    o: { ...EMPTY_OVERRIDES },
  },
  {
    key: "oi-only", label: "OI Change only",
    desc: "single indicator decides; no unanimity flicker",
    o: { ...EMPTY_OVERRIDES, indicators: ["oi_change"] },
  },
  {
    key: "mtf-only", label: "Multi-TF only",
    desc: "single indicator decides",
    o: { ...EMPTY_OVERRIDES, indicators: ["multi_tf"] },
  },
  {
    key: "ratio-only", label: "Ratio only",
    desc: "single indicator decides",
    o: { ...EMPTY_OVERRIDES, indicators: ["ratio"] },
  },
  {
    key: "zone-start", label: "Zone-start cadence",
    desc: "direction frozen at zone start — hunts live the whole zone",
    o: { ...EMPTY_OVERRIDES, cadence: "zone_start" },
  },
  {
    key: "hold-15", label: "Direction-hold 15m",
    desc: "every-candle signals, but hunts survive flickers 15 minutes",
    o: { ...EMPTY_OVERRIDES, directionHoldMin: 15 },
  },
  {
    key: "wide-open", label: "Wide open (max entries)",
    desc: "ratio-only + every-candle + 60m hold + band 1–100000 + 10 trades + one wide zone + retest on",
    // zone_start backfired here: one evaluation at 09:20 on ~5 minutes of
    // data → NO_TRADE all day (funnel-proven). every_candle + a long hold
    // keeps direction forming naturally while hunts survive the flicker.
    o: {
      ...EMPTY_OVERRIDES,
      indicators: ["ratio"],
      cadence: "every_candle",
      directionHoldMin: 60,
      premiumMin: 1,
      premiumMax: 100000,
      maxTrades: 10,
      singleWideZone: true,
      clearDayKills: true,
      umpEnableLong: true,
      umpEnableRetest: true,
    },
  },
];
