interface Props {
  /** The symbol the user is trying to switch to, or null when the dialog is closed. */
  pendingSymbol: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirmation gate for leaving the NIFTY 50 default on the main (live) dashboard.
 * The main dashboard is meant for NIFTY 50; changing to another symbol requires an
 * explicit confirmation. Once confirmed, switching stays unlocked for the session
 * (a page reload resets back to NIFTY). Rendered once at the App level.
 */
export function SymbolChangeConfirm({ pendingSymbol, onConfirm, onCancel }: Props) {
  if (!pendingSymbol) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onCancel}
    >
      <div
        className="panel p-6 flex flex-col gap-5 border border-accent/30 max-w-md w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold text-foreground">Switch away from NIFTY 50?</h2>
          <p className="text-sm text-muted">
            This dashboard is built for <span className="text-foreground font-medium">NIFTY 50</span>.
            Continue to <span className="text-foreground font-medium">{pendingSymbol}</span>? Reloading
            the page returns to NIFTY 50.
          </p>
        </div>

        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 rounded-lg border border-border text-sm text-muted
                       hover:text-foreground hover:border-accent/50 transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="px-5 py-2 rounded-lg bg-accent text-black font-semibold text-sm
                       hover:bg-accent/80 active:scale-95 transition-all"
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
