/**
 * ATM window stepper/input — no dropdown, so it never overlaps the chart.
 *   -1  → All strikes
 *    0  → ATM only
 *    N  → ATM ± N
 *
 * UX: [−] [ typed input ] [+]   plus a small "All" shortcut button
 */
import { useRef, useState } from "react";

interface Props {
  value: number;
  max?: number;
  onChange: (v: number) => void;
}

function displayLabel(v: number): string {
  if (v < 0) return "All";
  return String(v);
}

export function AtmWindowSelect({ value, max = 50, onChange }: Props) {
  const [draft, setDraft] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function clamp(n: number) {
    return Math.min(Math.max(n, 0), max);
  }

  function commitDraft(raw: string) {
    const t = raw.trim().replace(/^[±+]/,"");
    if (t === "" || t.toLowerCase() === "all") {
      onChange(-1);
    } else {
      const n = parseInt(t, 10);
      if (!isNaN(n)) onChange(clamp(n));
    }
    setDraft(null);
  }

  function step(delta: number) {
    if (value < 0) {
      // "All" → step down goes to max
      onChange(delta > 0 ? max : max);
    } else {
      const next = value + delta;
      if (next < 0) onChange(-1);          // wrap to "All"
      else onChange(clamp(next));
    }
  }

  const btnBase =
    "flex items-center justify-center w-7 h-7 rounded text-sm font-bold text-muted hover:text-foreground hover:bg-white/10 active:bg-white/20 transition-colors select-none focus:outline-none";

  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border bg-surface px-1 py-0.5 focus-within:border-accent transition-colors">
      {/* Decrement */}
      <button
        type="button"
        tabIndex={-1}
        onClick={() => step(-1)}
        className={btnBase}
        title="Fewer strikes"
      >
        −
      </button>

      {/* Typed input */}
      <input
        ref={inputRef}
        type="text"
        inputMode="numeric"
        value={draft !== null ? draft : displayLabel(value)}
        placeholder="5"
        className="w-12 bg-transparent text-center text-xs font-medium text-foreground focus:outline-none placeholder:text-muted py-0.5"
        onFocus={() => setDraft(displayLabel(value))}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter")   { commitDraft(draft ?? ""); inputRef.current?.blur(); }
          if (e.key === "Escape")  { setDraft(null); inputRef.current?.blur(); }
          if (e.key === "ArrowUp")   { e.preventDefault(); step(1); }
          if (e.key === "ArrowDown") { e.preventDefault(); step(-1); }
        }}
        onBlur={() => { if (draft !== null) commitDraft(draft); }}
      />

      {/* Increment */}
      <button
        type="button"
        tabIndex={-1}
        onClick={() => step(1)}
        className={btnBase}
        title="More strikes"
      >
        +
      </button>

      {/* "All" shortcut */}
      <button
        type="button"
        tabIndex={-1}
        onClick={() => onChange(-1)}
        className={`ml-0.5 px-1.5 h-6 rounded text-[10px] font-semibold transition-colors focus:outline-none ${
          value < 0
            ? "bg-accent text-black"
            : "text-muted hover:text-foreground hover:bg-white/10"
        }`}
        title="Show all strikes"
      >
        All
      </button>
    </div>
  );
}
