/** Client-side CSV / Excel export. CSV is hand-rolled (RFC-4180); Excel is a
 * lazy dynamic import so SheetJS-alternative `write-excel-file` never enters the
 * main bundle. Both operate on a simple `{ headers, rows }` table so any view
 * (by-strike snapshot, replay frames) can feed them.
 */
import type { SheetData } from "write-excel-file/browser";
import type { OIChangeResponse, ReplayFrame } from "../types";

export interface ExportTable {
  headers: string[];
  rows: (string | number | null)[][];
}

function csvEscape(v: string | number | null): string {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(table: ExportTable): string {
  const lines = [table.headers.map(csvEscape).join(",")];
  for (const r of table.rows) lines.push(r.map(csvEscape).join(","));
  return lines.join("\r\n");
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function downloadCsv(table: ExportTable, filename: string): void {
  triggerDownload(new Blob([toCsv(table)], { type: "text/csv;charset=utf-8" }), filename);
}

export async function downloadXlsx(table: ExportTable, filename: string): Promise<void> {
  try {
    const mod = await import("write-excel-file/browser");
    const writeXlsxFile = mod.default;
    const headerRow = table.headers.map((h) => ({ value: h, fontWeight: "bold" as const }));
    const dataRows = table.rows.map((r) =>
      r.map((c) =>
        typeof c === "number"
          ? { value: c, type: Number }
          : { value: c == null ? "" : String(c), type: String },
      ),
    );
    // Browser build: writeXlsxFile(sheetData) returns { toBlob, toFile }; toFile
    // triggers the download. Each row is an array of cell objects.
    const data = [headerRow, ...dataRows] as unknown as SheetData;
    await writeXlsxFile(data).toFile(filename);
  } catch {
    // Fall back to CSV if the Excel writer is unavailable or errors.
    downloadCsv(table, filename.replace(/\.xlsx$/i, ".csv"));
  }
}

/** By-strike OI-change snapshot → export table. */
export function buildSnapshotTable(data: OIChangeResponse): ExportTable {
  const headers = [
    "strike", "call_oi", "put_oi", "call_oi_change", "put_oi_change",
    "call_ltp", "put_ltp",
  ];
  const rows = data.rows.map((r) => [
    r.strike, r.call_oi, r.put_oi, r.call_oi_change, r.put_oi_change,
    r.call_ltp, r.put_ltp,
  ]);
  return { headers, rows };
}

/** Replay frames (per-frame summary) → export table. */
export function buildReplayTable(frames: ReplayFrame[]): ExportTable {
  const headers = [
    "timestamp", "spot", "atm", "total_call_oi", "total_put_oi",
    "total_call_oi_change", "total_put_oi_change", "ratio", "pcr",
  ];
  const rows = frames.map((f) => [
    f.ts, f.spot, f.atm, f.total_call_oi, f.total_put_oi,
    f.total_call_oi_change, f.total_put_oi_change,
    f.ratio == null ? null : Number(f.ratio.toFixed(4)),
    f.pcr == null ? null : Number(f.pcr.toFixed(4)),
  ]);
  return { headers, rows };
}
