import { useCallback } from "react";

const OPEN_MIN = 9 * 60 + 15; // 09:15 IST in minutes-from-midnight

/** Minutes-from-open (0 = 09:15) → "h:MM AM/PM". */
export function formatSessionMinute(min: number): string {
  const total = OPEN_MIN + Math.round(min);
  const h = Math.floor(total / 60);
  const m = total % 60;
  const ap = h >= 12 ? "PM" : "AM";
  const h12 = ((h + 11) % 12) + 1;
  return `${h12}:${String(m).padStart(2, "0")} ${ap}`;
}

interface Props {
  /** Selected window, in minutes from market open (09:15). */
  fromMin: number;
  toMin: number;
  /** Upper selectable bound — "now" while the session is live, else 375 (15:30). */
  maxMin: number;
  /** Whether custom-range mode is currently driving the chart. */
  active: boolean;
  onChange: (fromMin: number, toMin: number) => void;
  /** Clear the custom window and return to preset-timeframe mode. */
  onClear: () => void;
}

const MIN_GAP = 1; // minutes — keep the two handles from crossing

export function TimeRangeSlider({ fromMin, toMin, maxMin, active, onChange, onClear }: Props) {
  const span = Math.max(maxMin, 1);
  const leftPct = (fromMin / span) * 100;
  const widthPct = ((toMin - fromMin) / span) * 100;

  const handleFrom = useCallback(
    (v: number) => onChange(Math.min(v, toMin - MIN_GAP), toMin),
    [toMin, onChange],
  );
  const handleTo = useCallback(
    (v: number) => onChange(fromMin, Math.max(v, fromMin + MIN_GAP)),
    [fromMin, onChange],
  );

  return (
    <div className="flex flex-col gap-2 w-full">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted">
          Custom time range{" "}
          {active && (
            <span className="text-accent font-medium">
              · {formatSessionMinute(fromMin)} → {formatSessionMinute(toMin)}
              {toMin >= maxMin ? " (live)" : ""}
            </span>
          )}
        </span>
        {active && (
          <button
            type="button"
            onClick={onClear}
            className="text-[11px] text-muted hover:text-white underline underline-offset-2"
          >
            Clear
          </button>
        )}
      </div>

      <div className="flex items-center gap-3">
        <span className="text-[11px] text-muted w-16 shrink-0">{formatSessionMinute(fromMin)}</span>

        <div className="relative flex-1 h-6 flex items-center">
          {/* base track */}
          <div className="absolute h-1.5 w-full rounded-full bg-border" />
          {/* selected portion */}
          <div
            className={`absolute h-1.5 rounded-full ${active ? "bg-accent" : "bg-accent/40"}`}
            style={{ left: `${leftPct}%`, width: `${Math.max(widthPct, 0)}%` }}
          />
          <input
            type="range"
            min={0}
            max={maxMin}
            step={1}
            value={fromMin}
            onChange={(e) => handleFrom(Number(e.target.value))}
            className="range-input"
            aria-label="Range start time"
          />
          <input
            type="range"
            min={0}
            max={maxMin}
            step={1}
            value={Math.min(toMin, maxMin)}
            onChange={(e) => handleTo(Number(e.target.value))}
            className="range-input"
            aria-label="Range end time"
          />
        </div>

        <span className="text-[11px] text-muted w-16 shrink-0 text-right">
          {formatSessionMinute(toMin)}
        </span>
      </div>
    </div>
  );
}
