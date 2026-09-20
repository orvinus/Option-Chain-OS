/**
 * Per-engine action row ("check twice" controls, mounted on every indicator
 * dashboard): Save All Engine Settings (routes through the page's save flow —
 * the confirm-diff modal unless the Config Lock disabled it), Re-run Engine
 * Now (re-evaluates against stored data), Reset to Defaults (copies this
 * zone's engine section from the Appendix-A reference document).
 */
import type { Weekday, ZoneId } from "../../../types/algo";
import { Spinner } from "../../Loading";

interface Props {
  /** The panel's data-load flag, so the re-run button can show progress. */
  loading?: boolean;
  dirty: boolean;
  day: Weekday;
  zone: ZoneId;
  canReset: boolean;
  onRequestSave: () => void;
  onRerun: () => void;
  onResetDefaults: () => void;
  /** §9: open the copy-settings dialog scoped to this engine × day × zone. */
  onCopyTo?: () => void;
}

export function EngineActions({
  dirty, day, zone, canReset, onRequestSave, onRerun, onResetDefaults, onCopyTo,
  loading = false,
}: Props) {
  return (
    <div className="panel px-4 py-2.5 flex items-center gap-2 flex-wrap">
      <button
        type="button"
        className="pill pill-active text-xs disabled:opacity-50"
        disabled={!dirty}
        onClick={onRequestSave}
        title="Review the exact field diff, then commit as one new version"
      >
        💾 Save All Engine Settings
      </button>
      {/* `loading` comes from the panel's own data flag: re-running an engine
          took seconds with no feedback at all, so it read as a dead button. */}
      <button
        type="button"
        className="pill text-xs disabled:opacity-60"
        disabled={loading}
        onClick={onRerun}
      >
        {loading ? (
          <span className="inline-flex items-center gap-1.5">
            <Spinner size={10} />
            Re-running…
          </span>
        ) : (
          "▶ Re-run Engine Now"
        )}
      </button>
      <button
        type="button"
        className="pill text-xs text-ce border border-ce/30 disabled:opacity-50"
        disabled={!canReset}
        onClick={onResetDefaults}
        title="Copy this engine's Appendix-A reference parameters into the draft (still needs a Save)"
      >
        Reset {zone} to Defaults
      </button>
      {onCopyTo && (
        <button
          type="button"
          className="pill text-xs border border-accent/40"
          onClick={onCopyTo}
          title="Copy this engine's parameters from this zone onto any other day × zone (preview first; still needs a Save)"
        >
          Copy {zone} to…
        </button>
      )}
      <span className="text-[10.5px] text-muted">
        {dirty
          ? `unsaved changes for ${day} ${zone} — Save reviews the diff before anything goes live`
          : "all engine settings saved"}
      </span>
    </div>
  );
}
