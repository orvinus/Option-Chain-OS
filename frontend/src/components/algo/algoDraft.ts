/**
 * Draft/diff helpers for the Algo Config editor.
 *
 * The page keeps the server document (`envelope.config`) and an editable deep
 * clone (`draft`). Before the §13 confirm-then-commit save, the confirmation
 * modal shows exactly which dotted paths changed — the SAME flatten/diff shape
 * the backend audits with, so what the user confirms is what the audit log
 * records.
 */
import type { AlgoConfigDoc } from "../../types/algo";

export function cloneDoc(doc: AlgoConfigDoc): AlgoConfigDoc {
  return structuredClone(doc);
}

type Flat = Record<string, unknown>;

function flatten(value: unknown, prefix: string, out: Flat): void {
  if (Array.isArray(value)) {
    // Lists diff as one unit (mirrors backend flatten_config) so a reordered
    // rule list is one change row, not index-keyed noise.
    out[prefix] = JSON.stringify(value);
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      flatten(v, prefix ? `${prefix}.${k}` : k, out);
    }
    return;
  }
  out[prefix] = value;
}

export interface DocChange {
  path: string;
  before: unknown;
  after: unknown;
}

export function diffDocs(oldDoc: AlgoConfigDoc, newDoc: AlgoConfigDoc): DocChange[] {
  const a: Flat = {};
  const b: Flat = {};
  flatten(oldDoc, "", a);
  flatten(newDoc, "", b);
  const paths = new Set([...Object.keys(a), ...Object.keys(b)]);
  const changes: DocChange[] = [];
  for (const path of [...paths].sort()) {
    if (a[path] !== b[path]) {
      changes.push({ path, before: a[path], after: b[path] });
    }
  }
  return changes;
}

export function isDirty(oldDoc: AlgoConfigDoc, newDoc: AlgoConfigDoc): boolean {
  return diffDocs(oldDoc, newDoc).length > 0;
}

/** "days.monday.zones.Z1.premium_max" → "Monday · Z1 · premium max" */
export function humanizePath(path: string): string {
  const parts = path.split(".");
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  if (parts[0] === "days" && parts.length >= 2) {
    const day = cap(parts[1]);
    if (parts[2] === "zones" && parts.length >= 4) {
      return `${day} · ${parts[3]} · ${parts.slice(4).join(".").replace(/_/g, " ")}`;
    }
    return `${day} · ${parts.slice(2).join(".").replace(/_/g, " ")}`;
  }
  if (parts[0] === "global") {
    return `Global · ${parts.slice(1).join(".").replace(/_/g, " ")}`;
  }
  return path.replace(/_/g, " ");
}

export function shortValue(v: unknown): string {
  if (v === undefined) return "—";
  if (typeof v === "string") {
    return v.length > 48 ? `${v.slice(0, 45)}…` : v;
  }
  return String(v);
}
