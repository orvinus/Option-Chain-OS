interface Props {
  expiries: string[];
  value: string | null;
  onChange: (exp: string) => void;
  /** Non-null when the last expiry fetch threw an error. */
  error?: string | null;
}

function formatExpiry(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function placeholderText(expiries: string[], error: string | null | undefined): string {
  if (error) return "Error loading expiries";
  if (expiries.length === 0) return "Warming up…";
  return "";
}

export function ExpirySelect({ expiries, value, onChange, error }: Props) {
  const placeholder = placeholderText(expiries, error);

  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted">Expiry</span>
      <select
        className={[
          "bg-panel border rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-accent",
          error ? "border-red-500 text-red-400" : "border-border",
        ].join(" ")}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        disabled={expiries.length === 0}
        title={error ?? undefined}
      >
        {expiries.length === 0 && (
          <option value="">{placeholder}</option>
        )}
        {expiries.map((e) => (
          <option key={e} value={e}>
            {formatExpiry(e)}
          </option>
        ))}
      </select>
    </label>
  );
}
