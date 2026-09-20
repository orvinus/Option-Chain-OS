/**
 * Small shared controls for the Algo Config tab — a kill-switch style toggle,
 * labelled numeric/text/time inputs and a card shell, matching the reference
 * design (algo_config_full.html) on the platform's tailwind tokens.
 */
import type { ReactNode } from "react";

export function Card(props: { title?: ReactNode; hint?: string; children: ReactNode; className?: string }) {
  return (
    <div className={`panel px-4 py-3 ${props.className ?? ""}`}>
      {props.title != null && (
        <div className="flex items-center justify-between mb-2 pb-2 border-b border-border">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{props.title}</h3>
          {props.hint && <span className="text-[11px] text-muted/70">{props.hint}</span>}
        </div>
      )}
      {props.children}
    </div>
  );
}

/** Kill-switch toggle. `on` is the RAW boolean; pass `danger` when ON means
 * something is killed/disabled so it reads red instead of green. */
export function Switch(props: {
  on: boolean;
  onChange: (next: boolean) => void;
  danger?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const bg = props.on ? (props.danger ? "bg-ce" : "bg-pe") : "bg-gray-600";
  return (
    <button
      type="button"
      title={props.title}
      disabled={props.disabled}
      onClick={() => props.onChange(!props.on)}
      className={`relative w-9 h-5 rounded-full transition-colors flex-none ${bg} ${
        props.disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"
      }`}
    >
      <span
        className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${
          props.on ? "right-0.5" : "left-0.5"
        }`}
      />
    </button>
  );
}

export function ToggleLine(props: {
  name: string;
  desc?: string;
  on: boolean;
  onChange: (next: boolean) => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center justify-between py-2 border-b border-border/40 last:border-b-0 gap-3">
      <div>
        <div className="text-[12.5px] font-semibold text-gray-100">{props.name}</div>
        {props.desc && <div className="text-[10.5px] text-muted mt-0.5">{props.desc}</div>}
      </div>
      <Switch on={props.on} onChange={props.onChange} danger={props.danger} disabled={props.disabled} />
    </div>
  );
}

const inputCls =
  "bg-panel border border-border rounded-lg px-2 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-accent w-full";

export function NumField(props: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step?: number;
  suffix?: string;
  disabled?: boolean;
  width?: string;
}) {
  return (
    <label className={`block ${props.width ?? ""}`}>
      <span className="block text-[10px] text-muted mb-1">{props.label}</span>
      <div className="flex items-center gap-1">
        <input
          type="number"
          className={inputCls}
          value={Number.isFinite(props.value) ? props.value : 0}
          step={props.step ?? 1}
          disabled={props.disabled}
          onChange={(e) => {
            const v = parseFloat(e.target.value);
            if (Number.isFinite(v)) props.onChange(v);
          }}
        />
        {props.suffix && <span className="text-xs text-muted flex-none">{props.suffix}</span>}
      </div>
    </label>
  );
}

export function ColorField(props: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const validHex = /^#[0-9a-fA-F]{6}$/.test(props.value);
  return (
    <label className="block">
      <span className="block text-[10px] text-muted mb-1">{props.label}</span>
      <div className="flex items-center gap-1.5">
        <input
          type="color"
          className="w-9 h-8 bg-panel border border-border rounded-lg cursor-pointer p-0.5 flex-none"
          value={validHex ? props.value : "#FFD700"}
          onChange={(e) => props.onChange(e.target.value)}
        />
        <input
          type="text"
          className={inputCls}
          value={props.value}
          spellCheck={false}
          onChange={(e) => props.onChange(e.target.value)}
        />
      </div>
    </label>
  );
}

export function TimeField(props: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block">
      <span className="block text-[10px] text-muted mb-1">{props.label}</span>
      <input
        type="time"
        className={inputCls}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </label>
  );
}

export function DateField(props: {
  label?: string;
  value: string;
  onChange: (v: string) => void;
  /** Clamp the native calendar to the range the DB actually holds. */
  min?: string;
  max?: string;
}) {
  return (
    <label className="block">
      {props.label && <span className="block text-[10px] text-muted mb-1">{props.label}</span>}
      <input
        type="date"
        className={`${inputCls} cursor-pointer`}
        value={props.value}
        min={props.min}
        max={props.max}
        onChange={(e) => props.onChange(e.target.value)}
        onClick={(e) => {
          // Always pop the calendar — not only on the tiny native icon.
          const el = e.currentTarget as HTMLInputElement & { showPicker?: () => void };
          try {
            el.showPicker?.();
          } catch {
            /* browsers that gate showPicker on gestures still open natively */
          }
        }}
      />
    </label>
  );
}

export function SelectField<T extends string>(props: {
  label?: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <label className="block">
      {props.label && <span className="block text-[10px] text-muted mb-1">{props.label}</span>}
      <select
        className={inputCls}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value as T)}
      >
        {props.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function CalcNote(props: { children: ReactNode; tone?: "info" | "warn" }) {
  const tone =
    props.tone === "warn"
      ? "text-amber-300/90 bg-amber-950/40 border-amber-800/40"
      : "text-muted bg-panel/60 border-border";
  return (
    <div className={`text-[10.5px] leading-relaxed border rounded-lg px-2.5 py-1.5 ${tone}`}>
      {props.children}
    </div>
  );
}
