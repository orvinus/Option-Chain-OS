/**
 * Copy-settings helpers (§9) — pure functions over the draft document.
 *
 * Three scopes, one mutation entry point:
 *   day        → the whole DayConfig is replaced on each target day, zone
 *                START/END TIMES INCLUDED (`preserveTimes` defaults to false
 *                since 2026-09-18 — "copy Thursday to Monday" must produce a
 *                Monday identical to Thursday; the dialog's checkbox opts back
 *                into keeping the target's own windows). A whole day always
 *                carries a self-consistent, non-overlapping set of zones.
 *   zone       → `copyZoneInto`: band, trades, kills and ALL engine params of
 *                one zone onto any day×zone. Times travel only when the target
 *                is the SAME zone id on another day; copying Z1's 09:20–10:30
 *                onto Z3 would overlap that day's other zones, so there the
 *                target's own window is kept.
 *   indicator  → one engine section (oi_structure | mtf_ratio | mqae | ump)
 *                deep-cloned onto any day×zone.
 *
 * Everything stages in the draft — nothing is live until the page's Save flow
 * (confirm-diff modal) commits it, exactly as before.
 */
import type { AlgoConfigDoc, Weekday, ZoneId } from "../../types/algo";
import { WEEKDAY_LABEL, WEEKDAYS, ZONE_IDS } from "../../types/algo";
import type { DocChange } from "./algoDraft";
import { diffDocs } from "./algoDraft";

/** Engine sections of a ZoneConfig that can be copied on their own
 *  (same ids as `EngineKey` in engines/EnginesPanel — kept local so this
 *  module stays import-free of the React tree). */
export type CopyEngine = "oi_structure" | "mtf_ratio" | "mqae" | "ump";

export const COPY_ENGINE_LABEL: Record<CopyEngine, string> = {
  oi_structure: "OI Change",
  mtf_ratio: "Multi-TF",
  mqae: "Ratio",
  ump: "Ultra Master Pro",
};

export type CopyScope =
  | { kind: "day"; day: Weekday }
  | { kind: "zone"; day: Weekday; zone: ZoneId }
  | { kind: "indicator"; day: Weekday; zone: ZoneId; engine: CopyEngine };

export interface CopyTarget {
  day: Weekday;
  zone: ZoneId;
}

export interface CopyOptions {
  /** Day scope only: keep each target zone's own start/end (default FALSE —
   *  a day copy is a full copy, times included). */
  preserveTimes?: boolean;
}

export function deepClone<T>(x: T): T {
  return JSON.parse(JSON.stringify(x)) as T;
}

/** Copy one zone's config (premium band, trades, kills, ALL engine params)
 *  onto a target zone. Same zone id on another day copies the window too;
 *  a DIFFERENT zone id keeps the target's window (copying Z1's 09:20–10:30
 *  onto Z3 would otherwise create overlapping zones). */
export function copyZoneInto(
  doc: AlgoConfigDoc, sday: Weekday, sz: ZoneId, tday: Weekday, tz: ZoneId
): void {
  const src = doc.days[sday]?.zones[sz];
  const tgt = doc.days[tday]?.zones[tz];
  if (!src || !tgt) return;
  const clone = deepClone(src);
  if (sz !== tz) {
    // Cross-zone copy (e.g. Z1 → Z3): the source window would overlap the
    // target day's other zones, which the backend rejects at save. Keep the
    // target's own times; everything else copies.
    clone.start = tgt.start;
    clone.end = tgt.end;
  }
  doc.days[tday].zones[tz] = clone;
}

/** Whole-day copy; with `preserveTimes` the target's zone windows survive. */
export function copyDayInto(
  doc: AlgoConfigDoc, sday: Weekday, tday: Weekday, preserveTimes: boolean
): void {
  const src = doc.days[sday];
  if (!src || sday === tday) return;
  const prev = doc.days[tday];
  const clone = deepClone(src);
  if (preserveTimes && prev) {
    for (const z of ZONE_IDS) {
      const from = prev.zones[z];
      const to = clone.zones[z];
      if (from && to) {
        to.start = from.start;
        to.end = from.end;
      }
    }
  }
  doc.days[tday] = clone;
}

/** Copy one engine section onto a target zone. */
export function copyIndicatorInto(
  doc: AlgoConfigDoc, sday: Weekday, sz: ZoneId, engine: CopyEngine,
  tday: Weekday, tz: ZoneId
): void {
  const src = doc.days[sday]?.zones[sz]?.[engine];
  const tgt = doc.days[tday]?.zones[tz];
  if (!src || !tgt) return;
  // The four sections are distinct types; assign through the record shape so
  // TS does not require a per-engine branch for what is one deep-clone.
  (tgt as unknown as Record<CopyEngine, unknown>)[engine] = deepClone(src);
}

export function isSourceCell(scope: CopyScope, t: CopyTarget): boolean {
  if (scope.kind === "day") return t.day === scope.day;
  return t.day === scope.day && t.zone === scope.zone;
}

/** Day scope acts per DAY: many zone targets of one day collapse to one copy. */
export function effectiveTargets(scope: CopyScope, targets: CopyTarget[]): CopyTarget[] {
  const seen = new Set<string>();
  const out: CopyTarget[] = [];
  for (const t of targets) {
    if (isSourceCell(scope, t)) continue;
    const key = scope.kind === "day" ? t.day : `${t.day}:${t.zone}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(scope.kind === "day" ? { day: t.day, zone: "Z1" } : t);
  }
  return out;
}

/** Mutating entry point — call inside the page's `mutate(fn)`. */
export function applyCopy(
  doc: AlgoConfigDoc, scope: CopyScope, targets: CopyTarget[], opts: CopyOptions = {}
): void {
  const preserveTimes = opts.preserveTimes ?? false;
  for (const t of effectiveTargets(scope, targets)) {
    if (scope.kind === "day") {
      copyDayInto(doc, scope.day, t.day, preserveTimes);
    } else if (scope.kind === "zone") {
      copyZoneInto(doc, scope.day, scope.zone, t.day, t.zone);
    } else {
      copyIndicatorInto(doc, scope.day, scope.zone, scope.engine, t.day, t.zone);
    }
  }
}

export interface CopyPreviewRow {
  key: string;
  target: CopyTarget;
  label: string;
  changes: DocChange[];
}

export interface CopyPreview {
  rows: CopyPreviewRow[];
  total: number;
  warnings: string[];
}

function targetLabel(scope: CopyScope, t: CopyTarget): string {
  return scope.kind === "day" ? WEEKDAY_LABEL[t.day] : `${WEEKDAY_LABEL[t.day]} ${t.zone}`;
}

/** Non-mutating dry run: clone, apply, diff, bucket the changed paths per
 *  target. `warnings` flags index mismatches (a NIFTY zone's parameters
 *  landing on a SENSEX day still copies — the user just gets told). */
export function previewCopy(
  doc: AlgoConfigDoc, scope: CopyScope, targets: CopyTarget[], opts: CopyOptions = {}
): CopyPreview {
  const after = deepClone(doc);
  applyCopy(after, scope, targets, opts);
  const changes = diffDocs(doc, after);
  const eff = effectiveTargets(scope, targets);
  const rows: CopyPreviewRow[] = eff.map((t) => {
    const prefix = scope.kind === "day" ? `days.${t.day}.` : `days.${t.day}.zones.${t.zone}.`;
    return {
      key: scope.kind === "day" ? t.day : `${t.day}:${t.zone}`,
      target: t,
      label: targetLabel(scope, t),
      changes: changes.filter((c) => c.path.startsWith(prefix)),
    };
  });
  const warnings: string[] = [];
  const srcIndex = doc.days[scope.day]?.index_symbol;
  const mismatched = eff
    .filter((t) => doc.days[t.day]?.index_symbol !== srcIndex)
    .map((t) => `${WEEKDAY_LABEL[t.day]} (${doc.days[t.day]?.index_symbol ?? "?"})`);
  const uniqueMismatch = [...new Set(mismatched)];
  if (uniqueMismatch.length > 0) {
    warnings.push(
      scope.kind === "day"
        ? `Index changes: ${uniqueMismatch.join(", ")} will switch to ${srcIndex ?? "?"} — the whole day copies, including its index.`
        : `Index mismatch: ${srcIndex ?? "?"} parameters onto ${uniqueMismatch.join(", ")} — strike windows and premium bands may not suit that index.`
    );
  }
  return { rows, total: changes.length, warnings };
}

export function scopeLabel(scope: CopyScope): string {
  const d = WEEKDAY_LABEL[scope.day];
  if (scope.kind === "day") return `${d} (whole day)`;
  if (scope.kind === "zone") return `${d} ${scope.zone} (zone)`;
  return `${d} ${scope.zone} · ${COPY_ENGINE_LABEL[scope.engine]}`;
}

/** Every day×zone cell (the 5×3 target grid). */
export function allTargets(): CopyTarget[] {
  const out: CopyTarget[] = [];
  for (const day of WEEKDAYS) for (const zone of ZONE_IDS) out.push({ day, zone });
  return out;
}

export function targetKey(t: CopyTarget): string {
  return `${t.day}:${t.zone}`;
}
