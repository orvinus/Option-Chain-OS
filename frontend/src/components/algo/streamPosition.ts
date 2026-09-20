/**
 * The orchestrator's open position as the live stream ships it, plus the two
 * formatters every place that shows it uses — so the strip, P&L Summary, the
 * Paper Trading tiles and the broker card all print the same numbers.
 *
 * `current_price` / `unrealized_*` are marked at the position's OWN contract's
 * newest tick (backend `latest_contract_quote`), never at the strip's scoped
 * strike — see live_stream._status_block.
 */
import type { AlgoFrame } from "../../api/algoWs";

export interface StreamPosition {
  trade_id: number;
  side: string;
  contract: string;
  entry: number;
  lots: number;
  zone: string;
  ledger: string;
  sub_scenario?: string | null;
  lot_size?: number;
  strike?: number | null;
  option_type?: string | null;
  expiry?: string | null;
  entry_ts?: string | null;
  last_close?: number | null;
  current_price?: number | null;
  price_ts?: string | null;
  price_age_s?: number | null;
  unrealized_rupees?: number | null;
  unrealized_pct?: number | null;
}

export function streamPosition(frame: AlgoFrame | null | undefined): StreamPosition | null {
  const status = frame?.status as { position?: StreamPosition | null } | undefined;
  const p = status?.position;
  return p && typeof p === "object" && "trade_id" in p ? p : null;
}

/** ₹ with exactly two decimals and Indian grouping; "—" for missing. */
export function inr2(n: number | string | null | undefined, signed = false): string {
  const v = typeof n === "string" ? Number(n) : n;
  if (v == null || !Number.isFinite(v)) return typeof n === "string" && n !== "" ? n : "—";
  const abs = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sign = v < 0 ? "−" : signed && v > 0 ? "+" : "";
  return `${sign}₹${abs}`;
}

/** A plain number to at most two decimals (strings that are not numbers pass through). */
export function num2(n: unknown): string {
  if (n === null || n === undefined || n === "") return "—";
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v) || typeof n === "boolean") return String(n);
  if (Number.isInteger(v)) return String(v);
  return String(Math.round(v * 100) / 100);
}

export function pnlTone(n: number | null | undefined): string {
  if (n == null) return "text-gray-100";
  return n > 0 ? "text-pe" : n < 0 ? "text-ce" : "text-gray-100";
}

export function positionLabel(p: StreamPosition): string {
  return `${p.strike ?? ""}${p.option_type ?? ""}`;
}
