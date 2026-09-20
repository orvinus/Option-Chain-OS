/**
 * Shown when OUR OWN backend is unreachable (`/api/health` is not answering).
 *
 * This slot used to hold `ConnectBanner`, an MPIN + TOTP form left over from the
 * Angel One integration. It was doubly wrong: the XTS market-data session
 * authenticates with an appKey/secretKey held server-side — there is no per-user
 * credential to type — and the condition that rendered it is our backend being
 * down, which no login can fix. Nothing here asks the user for anything.
 */
export function BackendOfflineNotice() {
  return (
    <div className="panel p-6 flex flex-col items-center gap-3 text-center">
      <div className="w-8 h-8 rounded-full border-2 border-accent/40 border-t-accent animate-spin" />
      <div className="text-sm font-medium text-foreground">Waiting for the API…</div>
      <p className="text-xs text-muted max-w-md leading-relaxed">
        The dashboard cannot reach its backend, so nothing can load — not even stored
        history. This is a server-side problem, not a login one. Retrying automatically.
      </p>
    </div>
  );
}
