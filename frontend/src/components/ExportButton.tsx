import { useState } from "react";
import { downloadCsv, downloadXlsx, type ExportTable } from "../utils/exportData";

interface Props {
  /** Produce the export table on demand (null => nothing to export). */
  getTable: () => ExportTable | null;
  /** Filename without extension. */
  filenameBase: string;
  disabled?: boolean;
}

export function ExportButton({ getTable, filenameBase, disabled }: Props) {
  const [busy, setBusy] = useState(false);

  const doExport = async (kind: "csv" | "xlsx") => {
    const table = getTable();
    if (!table || table.rows.length === 0) return;
    setBusy(true);
    try {
      if (kind === "csv") downloadCsv(table, `${filenameBase}.csv`);
      else await downloadXlsx(table, `${filenameBase}.xlsx`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-1">
      <span className="text-xs text-muted">Export</span>
      <button
        type="button"
        className="pill"
        disabled={disabled || busy}
        onClick={() => void doExport("csv")}
        title="Download CSV"
      >
        CSV
      </button>
      <button
        type="button"
        className="pill"
        disabled={disabled || busy}
        onClick={() => void doExport("xlsx")}
        title="Download Excel"
      >
        Excel
      </button>
    </div>
  );
}
