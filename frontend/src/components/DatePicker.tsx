/** Historical trading-day picker.
 *
 * `value === null` means "Live / Today". A native <input type=date> gives a
 * calendar with min/max bounds; the adjacent <select> lists only days that
 * actually have stored data (a native date input can't disable arbitrary days).
 */
interface Props {
  value: string | null; // "YYYY-MM-DD" or null for live/today
  available: string[]; // trading days with data, newest first
  onChange: (date: string | null) => void;
}

export function DatePicker({ value, available, onChange }: Props) {
  const min = available.length ? available[available.length - 1] : undefined;
  const max = available.length ? available[0] : undefined;

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-muted">Date</span>
      <input
        type="date"
        value={value ?? ""}
        min={min}
        max={max}
        onChange={(e) => onChange(e.target.value || null)}
        className="bg-panel border border-border rounded-md px-2 py-1 text-xs text-foreground"
      />
      {available.length > 0 && (
        <select
          value={value && available.includes(value) ? value : ""}
          onChange={(e) => onChange(e.target.value || null)}
          className="bg-panel border border-border rounded-md px-2 py-1 text-xs text-foreground max-w-[150px]"
          title="Trading days with data"
        >
          <option value="">Live / Today</option>
          {available.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
      )}
      {value !== null && (
        <button type="button" className="pill" onClick={() => onChange(null)} title="Back to live">
          Live
        </button>
      )}
    </div>
  );
}
