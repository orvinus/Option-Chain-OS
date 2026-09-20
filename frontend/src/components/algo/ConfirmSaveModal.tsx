/**
 * §13 two-step save confirmation: shows exactly which dotted paths changed
 * (the same flatten/diff the backend audits), takes an optional note, and only
 * on explicit confirm does the caller POST the document. Cancel leaves the
 * previous saved settings completely untouched.
 */
import { useState } from "react";
import type { DocChange } from "./algoDraft";
import { humanizePath, shortValue } from "./algoDraft";

interface Props {
  changes: DocChange[];
  busy: boolean;
  errors: string[] | null;
  onConfirm: (note: string) => void;
  onCancel: () => void;
}

export function ConfirmSaveModal({ changes, busy, errors, onConfirm, onCancel }: Props) {
  const [note, setNote] = useState("");
  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center px-4"
      onClick={onCancel}
    >
      <div
        className="panel w-full max-w-xl px-5 py-4 max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-bold text-gray-100 mb-1">
          Are you sure you want to submit these settings?
        </h3>
        <p className="text-xs text-muted mb-3">
          {changes.length} field{changes.length === 1 ? "" : "s"} will change. The save is atomic —
          confirmed changes are written as one new config version and audited field-by-field.
        </p>
        <div className="flex-1 overflow-y-auto border border-border rounded-lg mb-3">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-panel">
              <tr className="text-muted text-left">
                <th className="px-2 py-1.5 font-medium">Setting</th>
                <th className="px-2 py-1.5 font-medium">Old</th>
                <th className="px-2 py-1.5 font-medium">New</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path} className="border-t border-border/40">
                  <td className="px-2 py-1.5 text-gray-200">{humanizePath(c.path)}</td>
                  <td className="px-2 py-1.5 text-ce/90 font-mono">{shortValue(c.before)}</td>
                  <td className="px-2 py-1.5 text-pe/90 font-mono">{shortValue(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {errors && errors.length > 0 && (
          <div className="text-xs text-ce bg-red-950/40 border border-red-800/40 rounded-lg px-3 py-2 mb-3 max-h-32 overflow-y-auto">
            <b>The save was rejected by validation:</b>
            <ul className="list-disc pl-4 mt-1">
              {errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </div>
        )}
        <input
          className="bg-panel border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent w-full mb-3"
          placeholder="Optional note for the version history (e.g. 'widened Wed Z2 band')"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex justify-end gap-2">
          <button type="button" className="pill" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="pill pill-active disabled:opacity-50"
            disabled={busy || changes.length === 0}
            onClick={() => onConfirm(note)}
          >
            {busy ? "Saving…" : "Confirm & Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
