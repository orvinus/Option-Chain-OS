import type { SymbolSectorGroup } from "../types";

interface Props {
  groups: SymbolSectorGroup[];
  value: string;
  onChange: (sym: string) => void;
  /** True while POST /api/active-symbol is in flight; disables the control + shows a spinner cue. */
  switching?: boolean;
  /** Last error from POST /api/active-symbol (if any). */
  error?: string | null;
}

export function SymbolSelect({ groups, value, onChange, switching, error }: Props) {
  const empty = groups.length === 0;
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted">Symbol</span>
      <select
        className={[
          "bg-panel border rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-accent min-w-[200px]",
          error ? "border-red-500 text-red-400" : "border-border",
          switching ? "opacity-60 cursor-wait" : "",
        ].join(" ")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={empty || switching}
        title={error ?? undefined}
      >
        {empty && <option value="">Loading…</option>}
        {groups.map((g) => (
          <optgroup key={g.sector} label={g.sector}>
            {g.symbols.map((s) => (
              <option key={s.symbol} value={s.symbol}>
                {s.display}
                {!s.fno_eligible ? " · spot only" : ""}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      {switching && (
        <svg className="w-4 h-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none" aria-label="Switching">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
        </svg>
      )}
    </label>
  );
}
