/**
 * Pure helpers for the UMP chart REPLAY (§4, 2026-09-09).
 *
 * One `/ump?history=true` fetch per (date, interval) returns the full stored
 * life plus the engine's level/state histories; every playhead move is then a
 * pure client-side derivation over that payload — never a network call.
 *
 * NO-LOOK-AHEAD RULE. Engine timestamps (events, histories) are 1-minute bar
 * STARTS; the engine learns a minute's bar only when it CLOSES, i.e. at
 * ts + 60 s. A display candle of `bucketSec` seconds starting at `t` is fully
 * known at t + bucketSec. So an engine fact stamped `ts` is visible at
 * playhead candle N iff chartTime(ts) + 60 <= candles[N].time + bucketSec.
 * At the 1-second display interval an entry at 10:35 therefore appears when
 * the playhead reaches 10:35:59 — the moment the engine actually saw it.
 */
import type { UTCTimestamp } from "lightweight-charts";
import type { UmpCandle, UmpEvalResponse, UmpStateRow } from "../../../types/algo";

const IST_OFFSET_SEC = 5.5 * 3600;

/** ISO-IST → the chart's "IST baked into UTC" epoch seconds (same as UmpTvChart). */
export function istToChartTime(iso: string): UTCTimestamp {
  const hasOffset = /[+-]\d{2}:\d{2}$|Z$/.test(iso);
  const epoch = Date.parse(hasOffset ? iso : `${iso}+05:30`);
  return (Math.floor(epoch / 1000) + IST_OFFSET_SEC) as UTCTimestamp;
}

/** Index of the last candle whose time is <= t (binary search); -1 if none. */
export function snapToCandle(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

/** Index of the first candle at/after the ISO-IST instant; clamps to the last. */
export function indexAtOrAfter(candles: UmpCandle[], iso: string): number {
  if (candles.length === 0) return 0;
  const t = istToChartTime(iso) as number;
  for (let i = 0; i < candles.length; i++) {
    if ((istToChartTime(candles[i].ts) as number) >= t) return i;
  }
  return candles.length - 1;
}

/** The instant (chart seconds) at which everything up to and including
 *  candle `idx` is known. */
export function visibleUntil(candles: UmpCandle[], idx: number, bucketSec: number): number {
  const c = candles[Math.max(0, Math.min(idx, candles.length - 1))];
  if (!c) return 0;
  return (istToChartTime(c.ts) as number) + Math.max(1, bucketSec);
}

/** Is an engine fact stamped at minute-bar start `ts` known by `until`? */
export function isVisible(ts: string, until: number): boolean {
  return (istToChartTime(ts) as number) + 60 <= until;
}

/** The level set as it stood at `iso` (last history row known by then). */
export function levelsAt(
  history: UmpEvalResponse["levels_history"] | undefined,
  iso: string,
): UmpEvalResponse["levels"] | null {
  if (!history || history.length === 0) return null;
  const until = (istToChartTime(iso) as number) + 60;
  let out: UmpEvalResponse["levels"] | null = null;
  for (const row of history) {
    if (isVisible(row.ts, until)) out = row.levels;
    else break;
  }
  return out ?? [];
}

function lastStateRow(rows: UmpStateRow[] | null | undefined, until: number): UmpStateRow | null {
  if (!rows) return null;
  let out: UmpStateRow | null = null;
  for (const r of rows) {
    if (isVisible(r.ts, until)) out = r;
    else break;
  }
  return out;
}

function zoneOf(b: number, zonePct: number) {
  const zp = zonePct;
  return {
    base: b,
    zone_top: b * (1 + zp / 100),
    upper_median: b * (1 + zp / 200),
    lower_median: b * (1 - zp / 200),
    zone_bottom: b * (1 - zp / 100),
  };
}

/** The sub-candles that fall inside display candle `idx`, in order. */
export function subsFor(data: UmpEvalResponse, idx: number): UmpCandle[] {
  const all = data.candles ?? [];
  const subs = data.subcandles ?? [];
  const c = all[idx];
  if (!c || subs.length === 0) return [];
  const start = istToChartTime(c.ts) as number;
  const end = start + (data.candles_interval || 60);
  return subs.filter((s) => {
    const t = istToChartTime(s.ts) as number;
    return t >= start && t < end;
  });
}

/** How many sub-steps display candle `idx` is made of (>= 1). */
export function subCount(data: UmpEvalResponse, idx: number): number {
  return Math.max(1, subsFor(data, idx).length);
}

/**
 * The partially formed bar: the first `n` sub-candles of display candle `idx`,
 * folded and stamped at the DISPLAY bucket's start so lightweight-charts
 * replaces the bar rather than appending a new one.
 */
export function formingBar(
  data: UmpEvalResponse,
  idx: number,
  n: number,
): { ts: string; o: number; h: number; l: number; c: number; coveredSec: number; lastSubTs: string } | null {
  const all = data.candles ?? [];
  const parent = all[idx];
  const subs = subsFor(data, idx);
  if (!parent || subs.length === 0) return null;
  const take = subs.slice(0, Math.max(1, Math.min(n, subs.length)));
  const subSec = data.subcandles_interval || 60;
  const last = take[take.length - 1];
  return {
    ts: parent.ts,
    o: take[0].o,
    h: Math.max(...take.map((s) => s.h)),
    l: Math.min(...take.map((s) => s.l)),
    c: last.c,
    // Everything up to the END of the last sub-candle is known.
    coveredSec:
      (istToChartTime(last.ts) as number) + subSec - (istToChartTime(parent.ts) as number),
    lastSubTs: last.ts,
  };
}

/**
 * Derive the response the panel/chart should render at playhead `idx`:
 * candles truncated, events/trades filtered (an unseen exit leaves the trade
 * open), the level ladder and engine state reconstructed from the histories,
 * and every derived field (active zone, Q-rungs, nearest levels, gate) rebuilt
 * with the engine's own formulas. Without histories (fetched without
 * `history=true`) levels/state fall back to the end-of-fetch values.
 */
export function deriveReplayView(
  data: UmpEvalResponse,
  idx: number,
  /** How many 1-minute sub-candles of candle idx+1 are revealed. Named
   *  ``subStep`` because ``sub`` is already the engine's sub-scenario below. */
  subStep = 0,
): UmpEvalResponse {
  const all = data.candles ?? [];
  if (all.length === 0) return data;
  const i = Math.max(0, Math.min(idx, all.length - 1));
  const bucketSec = data.candles_interval || 60;
  // The forming bar of candle i+1, grown from `subStep` sub-candles. The CLOSED
  // slice stays exactly as it was, so the chart's content key does not move
  // while the bar animates — rebuilding the whole series every frame is what
  // used to discard the in-progress candle (UmpTvChart setData guard).
  const forming = subStep > 0 ? formingBar(data, i + 1, subStep) : null;
  const until = forming
    ? (istToChartTime(forming.ts) as number) + forming.coveredSec
    : visibleUntil(all, i, bucketSec);
  const candles = all.slice(0, i + 1);
  const lastClose = forming ? forming.c : candles[candles.length - 1].c;
  const cursorIso = forming ? forming.lastSubTs : candles[candles.length - 1].ts;

  const events = (data.events ?? []).filter((e) => isVisible(e.ts, until));
  const trades = (data.trades ?? [])
    .filter((t) => isVisible(t.entry_ts, until))
    .map((t) =>
      t.exit_ts && !isVisible(t.exit_ts, until)
        ? { ...t, exit_ts: null, exit_price: null, exit_reason: "", pnl_points: null }
        : t,
    );

  const levels = levelsAt(data.levels_history, cursorIso) ?? data.levels;
  const st = lastStateRow(data.state_history, until);
  const inTrade = st ? st.in_trade : data.state === "IN_TRADE";
  const sub = st ? st.sub : data.sub_scenario;
  const entryPrice = st ? st.entry_price : data.entry_price;
  const baseLevel = st ? st.base_level : data.base_level;
  const maxSl = st ? st.max_sl : data.max_sl;
  const trailSl = st ? st.trail_sl : data.trail_sl;
  const sbTrail = st ? st.sb_trail_sl : data.sb_trail_sl;
  const sbStage = st ? st.sb_stage : data.sb_stage;
  const trig = st ? st.trig : data.state === "WATCHING";
  const levelsReady = st ? st.levels_ready : data.gate.levels_ready;

  const zonePct = Number((data.params as { visual?: { zone_width_pct?: number } })?.visual?.zone_width_pct ?? 3);
  const active_zone = inTrade && baseLevel != null ? zoneOf(baseLevel, zonePct) : null;

  const above = (p: number) => {
    let nb: number | null = null;
    for (const lv of levels) if (lv.price > p && (nb == null || lv.price < nb)) nb = lv.price;
    return nb;
  };
  const below = (p: number) => {
    let ns: number | null = null;
    for (const lv of levels) if (lv.price < p && (ns == null || lv.price > ns)) ns = lv.price;
    return ns;
  };
  let q_levels: UmpEvalResponse["q_levels"] = null;
  if (inTrade && baseLevel != null) {
    const nb = above(baseLevel);
    if (nb != null) {
      const q2 = (baseLevel + nb) / 2;
      q_levels = { q1: (baseLevel + q2) / 2, q2, q3: (q2 + nb) / 2, nb };
    }
  }
  const r = above(lastClose);
  const s = below(lastClose);
  const nearest_resistance =
    r != null && lastClose > 0
      ? { price: r, distance_pct: Math.round(((r - lastClose) / lastClose) * 10000) / 100 }
      : null;
  const nearest_support =
    s != null && lastClose > 0
      ? { price: s, distance_pct: Math.round(((lastClose - s) / lastClose) * 10000) / 100 }
      : null;

  return {
    ...data,
    candles,
    events,
    trades,
    levels,
    state: inTrade ? "IN_TRADE" : trig ? "WATCHING" : "IDLE",
    sub_scenario: sub,
    entry_price: inTrade ? entryPrice : null,
    base_level: inTrade ? baseLevel : null,
    max_sl: inTrade ? maxSl : null,
    trail_sl: inTrade ? trailSl : null,
    sb_trail_sl: inTrade ? sbTrail : null,
    sb_stage: inTrade ? sbStage : 0,
    active_zone,
    q_levels,
    nearest_resistance,
    nearest_support,
    gate: { ...data.gate, levels_ready: levelsReady },
    as_of: cursorIso,
    session_date: cursorIso.slice(0, 10),
  };
}

/** Next/previous weekday not in `holidays` (YYYY-MM-DD), clamped to [min,max]. */
export function stepTradingDay(
  date: string,
  delta: 1 | -1,
  holidays: string[],
  min?: string | null,
  max?: string | null,
): string | null {
  const d = new Date(`${date}T00:00:00Z`);
  for (let k = 0; k < 15; k++) {
    d.setUTCDate(d.getUTCDate() + delta);
    const iso = d.toISOString().slice(0, 10);
    if (min && iso < min) return null;
    if (max && iso > max) return null;
    const wd = d.getUTCDay();
    if (wd === 0 || wd === 6) continue;
    if (holidays.includes(iso)) continue;
    return iso;
  }
  return null;
}
