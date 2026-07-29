import type { ChartInterval } from "../utils/oiCandles";

interface Props {
  value: ChartInterval;
  onChange: (interval: ChartInterval) => void;
}

const ITEMS: { interval: ChartInterval; label: string }[] = [
  { interval: 1, label: "1 Min (Line)" },
  { interval: 5, label: "5 Min" },
  { interval: 10, label: "10 Min" },
  { interval: 15, label: "15 Min" },
  { interval: 30, label: "30 Min" },
];

/** Interval selector for the Charts page: 1m → line, 5/10/15/30m → candlesticks. */
export function ChartIntervalBar({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {ITEMS.map(({ interval, label }) => (
        <button
          key={interval}
          type="button"
          onClick={() => onChange(interval)}
          className={`pill ${value === interval ? "pill-active" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
