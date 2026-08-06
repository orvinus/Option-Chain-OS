/**
 * Non-blocking notice shown when the BROKER feed is down but our backend is fine.
 *
 * This used to be a full-screen "Connecting to broker…" panel that replaced the
 * entire dashboard, so a broker outage (or routine weekend maintenance) made every
 * stored snapshot unreachable — precisely when you most want to review history.
 * Stored data does not depend on the broker, so the pages now render normally and
 * simply flag that nothing is arriving live.
 *
 * Recovery is entirely server-side (the SessionSteward escalates on its own —
 * the browser no longer triggers logins), so this banner only informs.
 */
export function FeedOfflineBanner() {
  return (
    <div className="panel p-3 text-xs border border-amber-500/30 bg-amber-500/5 flex items-start gap-2">
      <svg className="w-4 h-4 shrink-0 mt-0.5 text-amber-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
      <span className="text-amber-200/95 leading-snug">
        <b>Live feed offline — showing stored data.</b>{" "}
        Historical charts and tables below are complete up to the last snapshot; only
        new ticks are missing. The server is recovering the feed automatically.
      </span>
    </div>
  );
}
