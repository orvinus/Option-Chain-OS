import type { Timeframe } from "../types";

interface Props {
  /** Active preset, or null when a custom range is driving the chart (no pill highlighted). */
  value: Timeframe | null;
  onChange: (tf: Timeframe) => void;
}

/** The canonical timeframe presets, in display order. Anything that shows a
 *  timeframe — these pills, the Multi-TF grid, the Replay grid — reads this list,
 *  so the same window is never called "1 Hr" in one place and "1 Hour" in another.
 *  Matches `DEFAULT_TIMEFRAMES` in `backend/app/api/multi_timeframe.py`; the
 *  sub-minute codes in the `Timeframe` union are deliberately absent (live-only). */
export const TIMEFRAME_ITEMS: { tf: Timeframe; label: string }[] = [
  { tf: "1m", label: "1 Min" },
  { tf: "3m", label: "3 Min" },
  { tf: "5m", label: "5 Min" },
  { tf: "10m", label: "10 Min" },
  { tf: "15m", label: "15 Min" },
  { tf: "30m", label: "30 Min" },
  { tf: "1h", label: "1 Hr" },
  { tf: "2h", label: "2 Hrs" },
  { tf: "3h", label: "3 Hrs" },
  { tf: "full_day", label: "Full Day" },
];

/** Timeframe code → display label, for tables that render backend-supplied codes. */
export const TF_LABEL: Record<string, string> = Object.fromEntries(
  TIMEFRAME_ITEMS.map(({ tf, label }) => [tf, label]),
);

export function TimeframeBar({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {TIMEFRAME_ITEMS.map(({ tf, label }) => (
        <button
          key={tf}
          type="button"
          onClick={() => onChange(tf)}
          className={`pill ${value === tf ? "pill-active" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
