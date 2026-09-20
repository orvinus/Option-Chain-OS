/**
 * Sub-tab — Validation Summary (§15 pre-save checklist + §14 runtime gate).
 *
 * Reads /api/algo/config/validation, which evaluates the SAME functions the
 * orchestrator's runtime safety gate uses — so what this panel shows green is
 * exactly what the engine will allow to trade, never a parallel opinion.
 */
import { useCallback, useEffect, useState } from "react";
import { algoApi } from "../../api/algoRest";
import type { AlgoConfigDoc, ValidationReport } from "../../types/algo";
import { Card } from "./controls";

export function ValidationPanel(props: {
  refreshKey: number;
  /** When set, validate THIS document (the Backtesting sandbox draft)
   *  instead of the live saved config. */
  document?: AlgoConfigDoc | null;
}) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { document: doc } = props;

  const load = useCallback(async () => {
    try {
      setReport(doc ? await algoApi.validateDocument(doc) : await algoApi.validation());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [doc]);

  useEffect(() => {
    void load();
  }, [load, props.refreshKey]);

  if (error) return <div className="text-sm text-ce">{error}</div>;
  if (!report) return <div className="text-sm text-muted">Evaluating…</div>;

  const blockedZones = Object.entries(report.zone_gate).filter(([, v]) => v.length > 0);

  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Pre-Save Checklist"
        hint={doc ? "evaluated against the SANDBOX draft (unsaved edits included)" : "evaluated against the LIVE saved config"}
      >
        {report.errors.length === 0 && report.warnings.length === 0 ? (
          <div className="text-sm text-pe">✔ All checks passed — the saved configuration is valid.</div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {report.errors.map((e) => (
              <div key={e} className="text-xs text-ce">✘ {e}</div>
            ))}
            {report.warnings.map((w) => (
              <div key={w} className="text-xs text-amber-300/90">⚠ {w}</div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Runtime Safety Gate — zone eligibility"
        hint="§14: an incomplete zone can never trade, live or paper"
      >
        {blockedZones.length === 0 ? (
          <div className="text-sm text-pe">
            ✔ All 15 zones pass the completeness gate and are eligible to trade (kill switches
            permitting).
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {blockedZones.map(([zone, problems]) => (
              <div key={zone} className="border border-ce/40 rounded-lg px-3 py-2 bg-red-950/20">
                <div className="text-xs font-bold text-ce mb-1">{zone} — BLOCKED FROM TRADING</div>
                {problems.map((p) => (
                  <div key={p} className="text-[11px] text-gray-300">• {p}</div>
                ))}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
