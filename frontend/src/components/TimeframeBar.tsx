import type { Timeframe } from "../types";

interface Props {
  /** Active preset, or null when a custom range is driving the chart (no pill highlighted). */
  value: Timeframe | null;
  onChange: (tf: Timeframe) => void;
}

const ITEMS: { tf: Timeframe; label: string }[] = [
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

export function TimeframeBar({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {ITEMS.map(({ tf, label }) => (
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
