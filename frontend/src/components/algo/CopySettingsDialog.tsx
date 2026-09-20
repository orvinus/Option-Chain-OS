/**
 * §9 Copy-settings dialog (markup pattern = ConfirmSaveModal): pick a scope
 * (whole day / one zone / one engine section), the source, then any set of
 * day×zone targets on the 5×3 grid. The preview lists exactly which dotted
 * paths change per target (same flatten/diff as the save confirmation), so the
 * user sees "Tuesday Z1 — 14 fields change" before touching the draft.
 * "Copy into draft" only mutates the draft — Save/lock/confirm flows are
 * untouched; nothing is live until the page's Save.
 */
import { useMemo, useState } from "react";
import type { AlgoConfigDoc, Weekday, ZoneId } from "../../types/algo";
import { WEEKDAY_LABEL, WEEKDAYS, ZONE_IDS } from "../../types/algo";
import { humanizePath, shortValue } from "./algoDraft";
import type { CopyEngine, CopyScope, CopyTarget } from "./copySettings";
import {
  COPY_ENGINE_LABEL,
  allTargets,
  applyCopy,
  effectiveTargets,
  isSourceCell,
  previewCopy,
  scopeLabel,
  targetKey,
} from "./copySettings";
import { SelectField } from "./controls";

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  initialScope: CopyScope;
  onClose: () => void;
}

const ENGINES = Object.keys(COPY_ENGINE_LABEL) as CopyEngine[];

/** Strip the target prefix so the expanded row reads "premium max", not
 *  "Tuesday · Z1 · premium max" fifteen times over. */
function relPath(path: string, scope: CopyScope): string {
  const parts = path.split(".");
  // days.<day>.zones.<zone>.<rest>  |  days.<day>.<rest>
  if (parts[0] === "days") {
    if (parts[2] === "zones" && scope.kind === "day") {
      return `${parts[3]} · ${parts.slice(4).join(".").replace(/_/g, " ")}`;
    }
    if (parts[2] === "zones") return parts.slice(4).join(".").replace(/_/g, " ");
    return parts.slice(2).join(".").replace(/_/g, " ");
  }
  return humanizePath(path);
}

export function CopySettingsDialog({ draft, mutate, initialScope, onClose }: Props) {
  const [kind, setKind] = useState<CopyScope["kind"]>(initialScope.kind);
  const [srcDay, setSrcDay] = useState<Weekday>(initialScope.day);
  const [srcZone, setSrcZone] = useState<ZoneId>(
    initialScope.kind === "day" ? "Z1" : initialScope.zone
  );
  const [engine, setEngine] = useState<CopyEngine>(
    initialScope.kind === "indicator" ? initialScope.engine : "oi_structure"
  );
  // Default OFF: "copy this day to those days" means an identical day, times
  // included (2026-09-18). Tick it to keep each target's own windows.
  const [preserveTimes, setPreserveTimes] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const scope: CopyScope = useMemo(() => {
    if (kind === "day") return { kind: "day", day: srcDay };
    if (kind === "zone") return { kind: "zone", day: srcDay, zone: srcZone };
    return { kind: "indicator", day: srcDay, zone: srcZone, engine };
  }, [kind, srcDay, srcZone, engine]);

  const targets: CopyTarget[] = useMemo(
    () => allTargets().filter((t) => selected.has(targetKey(t)) && !isSourceCell(scope, t)),
    [selected, scope]
  );
  const effective = useMemo(() => effectiveTargets(scope, targets), [scope, targets]);
  const preview = useMemo(
    () => previewCopy(draft, scope, targets, { preserveTimes }),
    [draft, scope, targets, preserveTimes]
  );

  const toggle = (keys: string[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const k of keys) {
        if (on) next.add(k);
        else next.delete(k);
      }
      return next;
    });
  const isOn = (t: CopyTarget) => selected.has(targetKey(t));
  const dayKeys = (d: Weekday) => ZONE_IDS.map((z) => targetKey({ day: d, zone: z }));
  const zoneKeys = (z: ZoneId) => WEEKDAYS.map((d) => targetKey({ day: d, zone: z }));
  const allKeys = allTargets().map(targetKey);

  const commit = () => {
    if (effective.length === 0) return;
    mutate((doc) => applyCopy(doc, scope, targets, { preserveTimes }));
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <div
        className="panel w-full max-w-2xl px-5 py-4 max-h-[88vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-bold text-gray-100 mb-1">Copy settings</h3>
        <p className="text-xs text-muted mb-3">
          Copies stage in the draft only — nothing is live until you Save (the diff is shown
          again there).
        </p>

        {/* ── Scope + source ── */}
        <div className="flex items-center gap-3 flex-wrap mb-3 text-xs">
          <span className="text-muted">Copy</span>
          {(
            [
              ["day", "whole day"],
              ["zone", "one zone"],
              ["indicator", "one indicator"],
            ] as [CopyScope["kind"], string][]
          ).map(([k, label]) => (
            <label key={k} className="flex items-center gap-1 cursor-pointer">
              <input
                type="radio"
                name="copy-scope"
                checked={kind === k}
                onChange={() => setKind(k)}
              />
              <span className={kind === k ? "text-gray-100 font-semibold" : "text-gray-300"}>
                {label}
              </span>
            </label>
          ))}
          <span className="text-muted ml-2">from</span>
          <div className="w-32">
            <SelectField
              value={srcDay}
              options={WEEKDAYS.map((w) => ({ value: w, label: WEEKDAY_LABEL[w] }))}
              onChange={setSrcDay}
            />
          </div>
          {kind !== "day" && (
            <div className="w-20">
              <SelectField
                value={srcZone}
                options={ZONE_IDS.map((z) => ({ value: z, label: z }))}
                onChange={setSrcZone}
              />
            </div>
          )}
          {kind === "indicator" && (
            <div className="w-40">
              <SelectField
                value={engine}
                options={ENGINES.map((e) => ({ value: e, label: COPY_ENGINE_LABEL[e] }))}
                onChange={setEngine}
              />
            </div>
          )}
        </div>

        {/* ── Target grid ── */}
        <div className="border border-border rounded-lg px-3 py-2 mb-3">
          <div className="flex items-center gap-2 flex-wrap text-[10.5px] mb-2">
            <span className="text-muted">Targets:</span>
            {kind !== "day" && (
              <button
                type="button"
                className="pill text-[10px] px-2 py-0.5"
                onClick={() => toggle(zoneKeys(srcZone), true)}
              >
                same zone ({srcZone}) on all days
              </button>
            )}
            {kind !== "day" && (
              <button
                type="button"
                className="pill text-[10px] px-2 py-0.5"
                onClick={() => toggle(dayKeys(srcDay), true)}
              >
                all zones on {WEEKDAY_LABEL[srcDay]}
              </button>
            )}
            <button
              type="button"
              className="pill text-[10px] px-2 py-0.5"
              onClick={() => toggle(allKeys, true)}
            >
              {kind === "day" ? "all days" : "all 15"}
            </button>
            <button
              type="button"
              className="pill text-[10px] px-2 py-0.5"
              onClick={() => toggle(allKeys, false)}
            >
              clear
            </button>
          </div>
          <table className="text-xs">
            <thead>
              <tr className="text-muted">
                <th className="text-left font-medium pr-3 pb-1"></th>
                {kind === "day" ? (
                  <th className="font-medium pb-1">whole day</th>
                ) : (
                  ZONE_IDS.map((z) => (
                    <th key={z} className="font-medium pb-1 px-3">
                      <button
                        type="button"
                        className="hover:text-accent"
                        title={`Select ${z} on every day`}
                        onClick={() => toggle(zoneKeys(z), true)}
                      >
                        {z}
                      </button>
                    </th>
                  ))
                )}
              </tr>
            </thead>
            <tbody>
              {WEEKDAYS.map((w) => (
                <tr key={w} className="border-t border-border/40">
                  <td className="py-1 pr-3 font-semibold text-gray-200">
                    {kind === "day" ? (
                      WEEKDAY_LABEL[w]
                    ) : (
                      <button
                        type="button"
                        className="hover:text-accent"
                        title={`Select all zones on ${WEEKDAY_LABEL[w]}`}
                        onClick={() => toggle(dayKeys(w), true)}
                      >
                        {WEEKDAY_LABEL[w]}
                      </button>
                    )}
                  </td>
                  {(kind === "day" ? (["Z1"] as ZoneId[]) : ZONE_IDS).map((z) => {
                    const t = { day: w, zone: z };
                    const src = isSourceCell(scope, t);
                    return (
                      <td key={z} className="text-center px-3">
                        <input
                          type="checkbox"
                          disabled={src}
                          title={src ? "source" : `${WEEKDAY_LABEL[w]} ${kind === "day" ? "" : z}`}
                          checked={!src && isOn(t)}
                          onChange={(e) => toggle([targetKey(t)], e.target.checked)}
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ── Options ── */}
        {kind === "day" && (
          <label className="flex items-center gap-2 text-xs mb-3 cursor-pointer">
            <input
              type="checkbox"
              checked={preserveTimes}
              onChange={(e) => setPreserveTimes(e.target.checked)}
            />
            <span className="text-gray-200">Keep each target day's own zone start/end times</span>
            <span className="text-muted">
              (default OFF — the copy replaces the whole day, zone times included)
            </span>
          </label>
        )}
        {kind === "zone" && (
          <div className="text-[10.5px] text-muted mb-3">
            A zone copied onto the SAME zone id on another day takes its window times too; onto a
            DIFFERENT zone id the target's own times are kept (the source window would overlap
            that day's other zones). Band, trades, kills and every engine's parameters always copy.
          </div>
        )}

        {/* ── Preview ── */}
        <div className="flex-1 min-h-0 overflow-y-auto border border-border rounded-lg mb-3">
          <div className="sticky top-0 bg-panel px-2 py-1.5 text-xs text-muted border-b border-border/40">
            Source <b className="text-gray-100">{scopeLabel(scope)}</b> →{" "}
            {effective.length === 0
              ? "pick at least one target"
              : `${effective.length} target${effective.length === 1 ? "" : "s"} · ${preview.total} field${preview.total === 1 ? "" : "s"} change`}
          </div>
          {preview.warnings.map((w) => (
            <div key={w} className="px-2 py-1.5 text-[11px] text-amber-300 border-b border-border/40">
              ⚠ {w}
            </div>
          ))}
          {preview.rows.map((row) => {
            const open = expanded.has(row.key);
            return (
              <div key={row.key} className="border-b border-border/40 last:border-b-0">
                <button
                  type="button"
                  className="w-full text-left px-2 py-1.5 text-xs flex items-center gap-2 hover:bg-panel/60"
                  onClick={() =>
                    setExpanded((prev) => {
                      const next = new Set(prev);
                      if (next.has(row.key)) next.delete(row.key);
                      else next.add(row.key);
                      return next;
                    })
                  }
                >
                  <span className="text-muted w-3">{open ? "▾" : "▸"}</span>
                  <span className="font-semibold text-gray-200">{row.label}</span>
                  <span className={row.changes.length === 0 ? "text-muted" : "text-pe/90"}>
                    — {row.changes.length === 0
                      ? "already identical"
                      : `${row.changes.length} field${row.changes.length === 1 ? "" : "s"} change`}
                  </span>
                </button>
                {open && row.changes.length > 0 && (
                  <table className="w-full text-[11px] mb-1">
                    <tbody>
                      {row.changes.map((c) => (
                        <tr key={c.path} className="border-t border-border/30">
                          <td className="px-2 py-1 pl-7 text-gray-200">{relPath(c.path, scope)}</td>
                          <td className="px-2 py-1 text-ce/90 font-mono">{shortValue(c.before)}</td>
                          <td className="px-2 py-1 text-pe/90 font-mono">{shortValue(c.after)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex justify-end gap-2">
          <button type="button" className="pill" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="pill pill-active disabled:opacity-50"
            disabled={effective.length === 0}
            onClick={commit}
            title="Apply to the draft (still needs a Save to go live)"
          >
            Copy into draft
          </button>
        </div>
      </div>
    </div>
  );
}
