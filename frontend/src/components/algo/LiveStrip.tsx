/**
 * The always-visible live bar for the Algo Config page.
 *
 * Two jobs, both of them honesty jobs:
 *
 * 1. **Say what the numbers are.** `source` is computed server-side from the
 *    same ground truth the trading staleness gate uses, so this badge and the
 *    gate that actually blocks entries can never disagree. A frozen page reads
 *    "REPLAY — last stored session", never a spinner and never a stale number
 *    dressed as a live one.
 * 2. **Say that the displayed signal is not the traded signal.** The user
 *    asked for intrabar readings; the orchestrator trades closed candles only.
 *    Rather than hide that, the strip renders LIVE and TRADED side by side and
 *    turns amber the moment they disagree.
 */
import type { AlgoFrame, AlgoStreamStatus } from "../../api/algoWs";
import { inr2, pnlTone, positionLabel, streamPosition } from "./streamPosition";

const INDICATOR_SHORT: Record<string, string> = {
  oi_change: "oi",
  multi_tf: "mtf",
  ratio: "ratio",
};
const DAY_SHORT: Record<string, string> = {
  monday: "Mon", tuesday: "Tue", wednesday: "Wed", thursday: "Thu", friday: "Fri",
};

const SOURCE_LABEL: Record<string, { text: string; cls: string }> = {
  live: { text: "● LIVE", cls: "text-pe" },
  stale: { text: "▲ STALE DATA", cls: "text-amber-400" },
  session_closed: { text: "■ MARKET CLOSED", cls: "text-muted" },
  replay: { text: "■ REPLAY — no live feed", cls: "text-amber-400" },
  other_symbol: { text: "▲ FEED ON ANOTHER SYMBOL", cls: "text-amber-400" },
  no_data: { text: "▲ NO DATA ARRIVING", cls: "text-ce" },
};

function readingOf(
  side: Record<string, { signal?: string; reading?: string }> | undefined,
  key: string,
): string {
  const r = side?.[key];
  return r?.signal ?? r?.reading ?? "—";
}

export function LiveStrip({
  frame,
  status,
  ageS,
  error,
}: {
  frame: AlgoFrame | null;
  status: AlgoStreamStatus;
  ageS: number;
  error: string | null;
}) {
  if (error) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-ce">
        <span className="font-bold">▲ LIVE STREAM</span>
        <span className="text-muted">{error}</span>
      </div>
    );
  }
  if (!frame) {
    return (
      <div className="text-[11px] text-muted">
        ○ live stream {status === "open" ? "waiting for first frame…" : status}
      </div>
    );
  }

  const src = SOURCE_LABEL[frame.source] ?? { text: frame.source, cls: "text-muted" };
  const rd = frame.readings;
  const intra = rd?.intrabar;
  const closed = rd?.closed;
  const diverged = !!rd?.diverged;
  const ltp = frame.price?.ltp;
  const pos = streamPosition(frame);
  // Only the filters this zone trades on. An older backend without the field
  // falls back to all three rather than showing nothing.
  const enabled = frame.enabled_indicators?.length
    ? frame.enabled_indicators
    : ["oi_change", "multi_tf", "ratio"];
  const scopeLabel = `${DAY_SHORT[frame.scope.day] ?? frame.scope.day} ${frame.scope.zone}`;
  const activeZone = (frame.status as { active_zone?: string } | undefined)?.active_zone ?? "";
  const [activeDay, activeZoneId] = activeZone.split("/");
  const tradingElsewhere =
    !!activeZone && (activeDay !== frame.scope.day || activeZoneId !== frame.scope.zone);

  return (
    <div className="flex items-center gap-3 flex-wrap text-[11px]">
      <span className={`font-bold ${src.cls}`}>{src.text}</span>

      {frame.strike != null && (
        <span className="text-muted">
          {frame.scope.symbol} {frame.strike}
          {frame.scope.option_type}
          <span className="text-[9.5px] ml-1 opacity-70">({frame.strike_source})</span>
        </span>
      )}

      {ltp != null && (
        <span className="font-semibold text-gray-100 tabular-nums">₹{ltp.toFixed(2)}</span>
      )}

      {frame.frozen_at && (
        <span className="text-muted" title="The last bar this contract actually printed.">
          frozen at {frame.frozen_at.slice(11, 16)}
        </span>
      )}

      {intra && (
        <span className="flex items-center gap-1.5">
          <span
            className={`px-1 py-0.5 rounded font-bold ${
              diverged ? "bg-amber-500/20 text-amber-300" : "bg-accent/20 text-accent"
            }`}
            title="Recomputed on the FORMING candle — display only. The engine does not trade on this."
          >
            {diverged ? "LIVE ≠ TRADED" : "LIVE"}
          </span>
          <span className="text-gray-100 font-semibold">{intra.combined ?? "—"}</span>
          <span
            className="text-[9.5px] text-muted"
            title={`Readings for ${scopeLabel} — only the ${enabled.length} filter(s) that zone trades on.`}
          >
            {enabled.map((k) => `${INDICATOR_SHORT[k] ?? k} ${readingOf(intra, k)}`).join(" · ")}
            <span className="ml-1 opacity-70">({scopeLabel})</span>
            {tradingElsewhere && (
              <span className="ml-1 text-amber-400/90" title="The engine is trading a different zone right now.">
                · engine on {DAY_SHORT[activeDay] ?? activeDay} {activeZoneId}
              </span>
            )}
          </span>
        </span>
      )}

      {closed && (
        <span
          className="flex items-center gap-1.5"
          title="The CLOSED-candle basis — this is what the orchestrator actually trades on."
        >
          <span className="px-1 py-0.5 rounded bg-white/10 text-muted font-bold">TRADED</span>
          <span className="text-gray-100 font-semibold">{closed.combined ?? "—"}</span>
          {rd?.closed_as_of && (
            <span className="text-[9.5px] text-muted">@ {rd.closed_as_of.slice(11, 16)}</span>
          )}
        </span>
      )}

      {pos && (
        <span
          data-testid="strip-position"
          className={`flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 ${
            pos.ledger === "live" ? "border-ce/60 bg-ce/10" : "border-accent/50 bg-accent/10"
          }`}
          title={
            `Open ${pos.ledger.toUpperCase()} position — trade #${pos.trade_id}, ${pos.zone}` +
            (pos.entry_ts ? `, entered ${pos.entry_ts.slice(11, 16)}` : "") +
            (pos.price_ts ? ` · price as of ${pos.price_ts.slice(11, 19)}` : "") +
            (pos.price_age_s != null ? ` (${Math.round(pos.price_age_s)}s old)` : "")
          }
        >
          <span className={`font-bold ${pos.ledger === "live" ? "text-ce" : "text-accent"}`}>
            IN TRADE · {pos.ledger.toUpperCase()}
          </span>
          <span className={`font-bold ${pos.side === "CALL" ? "text-pe" : "text-ce"}`}>{pos.side}</span>
          <span className="text-gray-100 font-semibold">
            {positionLabel(pos)} × {pos.lots}
          </span>
          <span className="text-muted tabular-nums">
            in {pos.entry.toFixed(2)}
            {pos.current_price != null && <> → {pos.current_price.toFixed(2)}</>}
          </span>
          {pos.unrealized_rupees != null && (
            <span className={`font-bold tabular-nums ${pnlTone(pos.unrealized_rupees)}`}>
              {inr2(pos.unrealized_rupees, true)}
              {pos.unrealized_pct != null && (
                <span className="font-normal"> ({pos.unrealized_pct > 0 ? "+" : ""}{pos.unrealized_pct.toFixed(1)}%)</span>
              )}
            </span>
          )}
          <span className="text-[9.5px] text-muted">#{pos.trade_id}</span>
        </span>
      )}

      <span className="text-[9.5px] text-muted" title="Seconds since the last frame arrived.">
        {status === "open" ? `${ageS}s ago` : status}
      </span>
    </div>
  );
}
