/**
 * NIFTY Ultra Master Pro dashboard — the execution engine's view, bound to
 * one zone's configuration.
 *
 * Mirrors the reference (ultra_master_pro_zone_dashboard.html) with one
 * deliberate difference: the "Regime & Technical Context" card (EMA 20/50/200
 * stack) is REMOVED on explicit instruction. Everything else is here: the
 * Data-Ready Gate strip, the premium chart with the level ladder overlaid,
 * the Entry Model State Machine, the Exit & Trail system (P1/P2/P2b/P3/P4
 * with the Q-rungs and System B), the Level Ladder, Zone Sub-Levels, the
 * Trade History log, and the full 5-group Pine-inputs editor for the
 * selected zone (edits flow through the same draft → confirm-save workflow).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ENTRY_STYLE, UmpTvChart } from "./UmpTvChart";
import { algoApi } from "../../../api/algoRest";
import type {
  AlgoConfigDoc,
  UmpDisplayInterval,
  UmpEvalResponse,
  UmpParams,
  Weekday,
  ZoneId,
} from "../../../types/algo";
import { UMP_DISPLAY_INTERVALS, UMP_SCENARIOS, exitReasonLabel } from "../../../types/algo";
import { ReplayController } from "../../ReplayController";
import {
  deriveReplayView, formingBar, subCount, subsFor, indexAtOrAfter, seekPosition, stepTradingDay,
} from "./umpReplay";

const INTERVAL_KEY = "ump.displayInterval";

// HIDDEN-2026-09-11 (user request): only 1m · 5m · 15m · 1h · 1d · 1W have
// pills in UmpTvChart.tsx; every other interval is hidden. A browser that
// already stored one of the hidden ones would otherwise come back on an
// interval with no pill showing it, so they fall back to "entry" here.
// TO UNLOCK ALL TIMEFRAMES: delete this constant and the
// `HIDDEN_INTERVALS.includes(v)` term below, and restore the full rows in
// UmpTvChart.tsx. The backend serves every one of these either way.
const HIDDEN_INTERVALS: readonly string[] = [
  "1s", "5s", "15s", "30s",   // seconds
  "3m", "10m", "30m",         // minutes outside 1m/5m/15m
  "2h", "3h",                 // hours other than 1h (1d and 1W are shown)
];

function loadInterval(): UmpDisplayInterval {
  try {
    const v = window.localStorage.getItem(INTERVAL_KEY) as UmpDisplayInterval | null;
    if (v && HIDDEN_INTERVALS.includes(v)) return "entry";   // HIDDEN-2026-09-11
    return v && (UMP_DISPLAY_INTERVALS as readonly string[]).includes(v) ? v : "entry";
  } catch {
    return "entry";
  }
}
import { CalcNote, Card, ColorField, NumField, SelectField, Switch } from "../controls";
import { useAlgoStreamContext } from "../../../hooks/useAlgoStream";
import { EngineActions } from "./EngineActions";
import { LoadingBlock, Spinner } from "../../Loading";

const TYPE_COLOR: Record<number, string> = {
  1: "#ef4444", // 1H-STRUCT
  2: "#22c55e", // DISCOVERY
  3: "#22d3ee", // BRIDGE
  4: "#eab308", // MEDIAN
};

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  day: Weekday;
  zone: ZoneId;
  dirty: boolean;
  histDate: string;
  expiry: string;
  at: string;       // "" = whole session; "HH:MM" freezes the eval
  configVersion: number | null;   // pin the eval to a saved config version
  configRun?: number | null;      // pin to a backtest run's frozen config
  configSandbox?: boolean;        // evaluate under the Backtesting sandbox doc
  livePoll?: boolean;             // false in the workspace: no auto-refresh
  defaults: AlgoConfigDoc | null;
  onRequestSave: () => void;
  /** A replay trade's contract — seeds the strike selector (QA M4). */
  initialStrike?: number | null;
  initialOptionType?: "CE" | "PE" | null;
  onCopyTo?: () => void;          // §9 copy-settings dialog (EnginesPanel owns it)
}

/** Mon–Fri 09:15–15:30 IST by the browser clock (holidays aside) — only
 *  decides whether the live-candle status line is worth showing. */
function nseSessionNowIst(): boolean {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  if (get("weekday") === "Sat" || get("weekday") === "Sun") return false;
  const hm = Number(get("hour")) * 60 + Number(get("minute"));
  return hm >= 9 * 60 + 15 && hm <= 15 * 60 + 30;
}

export function UmpPanel({
  draft, mutate, day, zone, dirty, histDate, expiry, at, configVersion,
  configRun, configSandbox, livePoll = true, defaults, onRequestSave,
  initialStrike, initialOptionType, onCopyTo,
}: Props) {
  const [fetched, setFetched] = useState<UmpEvalResponse | null>(null);
  // Which request produced `fetched` ("live" or "replay:<date>:<interval>").
  // Replay must never run on a payload fetched for something else: starting a
  // replay kept the OLD live payload on screen for the ~2 s the history load
  // takes, and the transport stepped through it in whole 5-minute jumps
  // (09:20 → 09:25 → 09:30) and spent the seek target on it (2026-09-23).
  const [fetchedFor, setFetchedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The /ump call replays the contract's ENTIRE stored life minute by minute
  // and takes seconds. Without this flag the panel filled the wait with
  // confident falsehoods: "No trades in the contract's stored life", "not
  // armed", "idle", "—".
  const [loading, setLoading] = useState(true);
  // Display interval (§3) — persisted per browser; never changes the engine.
  const [interval, setIntervalState] = useState<UmpDisplayInterval>(loadInterval);
  const setInterval_ = useCallback((v: UmpDisplayInterval) => {
    setIntervalState(v);
    try {
      window.localStorage.setItem(INTERVAL_KEY, v);
    } catch {
      /* private mode */
    }
  }, []);
  // Replay (§4): one fetch per (date, interval) with histories; every
  // playhead move is a pure client-side derivation (umpReplay.ts).
  const [replay, setReplay] = useState<{ date: string } | null>(null);
  const [replayDateInput, setReplayDateInput] = useState<string>("");
  const [replayTimeInput, setReplayTimeInput] = useState<string>("09:20");
  const [replayOpen, setReplayOpen] = useState(false);
  const [index, setIndex] = useState(0);
  // How many 1-minute sub-candles of the NEXT display candle are revealed.
  // 0 = sitting exactly on a closed candle. This is what makes the bar grow
  // instead of appearing whole (2026-09-13).
  const [sub, setSub] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(5);
  const seekToRef = useRef<string | null>(null);
  const totalRef = useRef(0);
  const [optionType, setOptionType] = useState<"CE" | "PE">("CE");
  // null = automatic (the zone's premium-band pick — what the orchestrator
  // actually hunts); a number = explicit contract override.
  const [strikeOverride, setStrikeOverride] = useState<number | null>(null);
  // Key-levels panel focus mode — Pine's table shows the 7 nearest by default.
  const [nearestOnly, setNearestOnly] = useState(true);
  const params = draft.days[day]?.zones[zone]?.ump;

  // The live forming 5-minute bar, when a stream is available AND this panel is
  // showing the live cursor. A pinned historical date / `at` minute must stay
  // frozen, and the Backtesting workspace has no provider at all, so both fall
  // through to the REST snapshot untouched.
  const stream = useAlgoStreamContext();
  const frozen = !!histDate || !!at || !livePoll || !!replay;
  const f = stream?.frame;
  // The forming bar only makes sense when the display candles share the
  // stream's bucket: the entry-TF bar (b5, bucketed on tf_min) or the 1m bar.
  const streamTf = (f?.candle?.tf_min ?? 5) * 60;
  const sameContract =
    !!f && !!fetched &&
    f.scope.symbol === fetched.symbol &&
    f.strike === fetched.strike &&
    f.scope.option_type === fetched.option_type;
  const liveBar = (() => {
    if (frozen || !f?.candle || !fetched || !sameContract) return null;
    const bucket = fetched.candles_interval || fetched.entry_timeframe_min * 60;
    if (bucket === streamTf && f.candle.b5) return { ts: f.candle.b5_start, ...f.candle.b5 };
    if (bucket === 60 && f.candle.m1) return { ts: f.candle.m1_start, ...f.candle.m1 };
    return null;
  })();

  // Keep the page's stream on the contract this chart shows (see
  // AlgoConfigPage). Released when the chart is frozen or unmounts.
  const pinContract = stream?.pinContract;
  const pinStrike = !frozen && fetched ? fetched.strike : null;
  const pinOt = !frozen && fetched ? (fetched.option_type as "CE" | "PE") : null;
  useEffect(() => {
    if (!pinContract) return;
    pinContract(pinStrike != null && pinOt ? { strike: pinStrike, optionType: pinOt } : null);
  }, [pinContract, pinStrike, pinOt]);
  useEffect(() => () => pinContract?.(null), [pinContract]);

  /** Why the live forming candle is or is not moving — never a silent freeze. */
  const liveCandle = (() => {
    // Only while NSE is actually trading — off-hours there is no live candle
    // to have, and saying "OFF" then would be noise.
    if (frozen || !fetched || !stream || !nseSessionNowIst()) return null;
    if (liveBar && stream.ageS <= 15) return { on: true, text: "Live candle ON" };
    const bucket = fetched.candles_interval || fetched.entry_timeframe_min * 60;
    let why: string;
    if (!f || stream.status !== "open") why = "the live stream is not connected";
    else if (stream.ageS > 15) why = `no live update for ${stream.ageS} s`;
    else if (!sameContract) why = "the live stream is switching to this contract";
    else if (!f.candle) why = "no live price for this contract yet";
    else if (bucket !== streamTf && bucket !== 60)
      why = `a ${Math.round(bucket / 60)}-minute chart cannot use the ${Math.round(streamTf / 60)}-minute live candle — pick ${Math.round(streamTf / 60)}m or 1m`;
    else why = "no live candle for this interval";
    return { on: false, text: `Live candle OFF — ${why}. The chart still updates every 30 s.` };
  })();

  // What the panel RENDERS: the fetched evaluation, or the replay view at
  // the playhead (candles truncated, levels/state/events reconstructed).
  const replayKey = replay ? `replay:${replay.date}:${interval}` : null;
  const replayReady = !!replay && !!fetched && fetchedFor === replayKey;
  const data = useMemo(
    () => (replay ? (fetched && replayReady ? deriveReplayView(fetched, index, sub) : null) : fetched),
    [fetched, replay, replayReady, index, sub],
  );

  /** A load is running and there is nothing trustworthy to show yet. */
  const waiting = loading && !data;

  /** Move the playhead by ±n SUB-candles, rolling across display buckets. */
  const skipSub = (delta: number) => {
    const f = fetchedRef.current;
    const total = f?.candles?.length ?? 0;
    if (!f || total === 0) return;
    let i = indexRef.current;
    let k = subRef.current + delta;
    while (k < 0) {
      if (i === 0) {
        k = 0;
        break;
      }
      i -= 1;
      k += subCount(f, i + 1);
    }
    for (;;) {
      const n = subCount(f, i + 1);
      if (k < n || i >= total - 1) break;
      k -= n;
      i += 1;
    }
    if (i >= total - 1) k = 0;
    setIndex(i);
    setSub(Math.max(0, k));
  };

  /** The instant the playhead is actually at — the SUB-candle's stamp while a
   *  bar is forming, else the closed candle's. */
  const replayCursorTs = useMemo(() => {
    const cs = fetched?.candles ?? [];
    if (!fetched || cs.length === 0) return null;
    if (sub > 0) {
      const f = formingBar(fetched, index + 1, sub);
      if (f) return f.lastSubTs;
    }
    // A CLOSED candle is known through its LAST minute — show that, not the
    // candle's start. Showing the start made the 5th minute of every 5-minute
    // candle vanish and the clock step backwards: 09:43 → 09:40 → 09:45
    // (reported 2026-09-23). Now: 09:40 41 42 43 44 45.
    const i = Math.min(index, cs.length - 1);
    const subs = subsFor(fetched, i);
    return subs.length ? subs[subs.length - 1].ts : cs[i]?.ts ?? null;
  }, [fetched, index, sub]);

  // The forming bar during replay, applied incrementally by the chart exactly
  // as the live one is. Null when sitting on a closed candle.
  const replayBar = useMemo(() => {
    if (!fetched || !replayReady || sub <= 0) return null;
    const f = formingBar(fetched, index + 1, sub);
    return f ? { ts: f.ts, o: f.o, h: f.h, l: f.l, c: f.c } : null;
  }, [fetched, replayReady, index, sub]);

  // Request-sequence guard: a context change fires two overlapping fetches
  // (the reset effect + the load effect); without this the STALE response
  // can land last and display the wrong contract's evaluation.
  const loadSeq = useRef(0);

  // `background: true` = the 30s poll. It must NOT raise the loading flag, or
  // the panel blinks every 30 seconds over data that is already correct.
  const load = useCallback(async (opts?: { background?: boolean }) => {
    const seq = ++loadSeq.current;
    const forKey = replay ? `replay:${replay.date}:${interval}` : "live";
    if (!opts?.background) setLoading(true);
    try {
      const res = await algoApi.umpEval({
        day,
        zone,
        date: replay ? replay.date : histDate || undefined,
        expiry: expiry || undefined,
        optionType,
        strike: strikeOverride ?? undefined,
        at: replay ? undefined : at || undefined,
        configVersion: configVersion ?? undefined,
        configRun: configRun ?? undefined,
        configSandbox: configSandbox || undefined,
        interval,
        history: !!replay,
      });
      if (seq !== loadSeq.current) return;
      setFetched(res);
      setFetchedFor(forKey);
      setError(null);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setFetched(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      // Only the NEWEST request may lower the flag. A superseded response
      // landing late must not hide the indicator for one still in flight —
      // a context change fires two overlapping /ump calls (see loadSeq).
      if (seq === loadSeq.current) setLoading(false);
    }
  }, [day, zone, histDate, expiry, optionType, strikeOverride, at, configVersion, configRun, configSandbox, interval, replay]);

  // A deep link (replay trade → this panel) pins the traded contract. The
  // ref survives the context-reset effect below, which would otherwise clear
  // the strike the moment the option type it just set is applied.
  const pinnedInit = useRef<{ strike: number; ot: "CE" | "PE" } | null>(null);

  // A pinned strike belongs to one (day, zone, side, date) context — clear
  // the override whenever that context changes (unless a deep link is
  // pending for exactly this context).
  useEffect(() => {
    const pending = pinnedInit.current;
    if (pending && pending.ot === optionType) {
      pinnedInit.current = null;
      setStrikeOverride(pending.strike);
      return;
    }
    setStrikeOverride(null);
  }, [day, zone, optionType, histDate, expiry]);

  useEffect(() => {
    if (initialStrike == null) return;
    const ot = initialOptionType ?? "CE";
    pinnedInit.current = { strike: initialStrike, ot };
    setOptionType(ot);
    setStrikeOverride(initialStrike);
  }, [initialStrike, initialOptionType]);

  useEffect(() => {
    void load();
    if (histDate || at || !livePoll || replay) return undefined;
    const t = window.setInterval(() => void load({ background: true }), 30_000);
    return () => window.clearInterval(t);
  }, [load, histDate, at, livePoll, replay]);

  // Seed the playhead once the replay fetch lands (start / day step /
  // interval switch each leave the wanted instant in seekToRef).
  useEffect(() => {
    if (!fetched || !replayReady) return;
    const total = fetched.candles?.length ?? 0;
    totalRef.current = total;
    const want = seekToRef.current;
    if (want) {
      seekToRef.current = null;
      const pos = seekPosition(fetched, want);
      setIndex(pos.index);
      setSub(pos.sub);
    } else {
      setIndex((i) => Math.min(i, Math.max(0, total - 1)));
    }
  }, [fetched, replayReady]);

  // rAF playback loop — speed = display bars per second.
  const playRef = useRef({ playing, speed });
  playRef.current = { playing, speed };
  const indexRef = useRef(index);
  indexRef.current = index;
  const fetchedRef = useRef(fetched);
  fetchedRef.current = fetched;
  const subRef = useRef(sub);
  subRef.current = sub;
  useEffect(() => {
    if (!playing) return undefined;
    let raf = 0;
    let last = performance.now();
    let acc = 0;
    const tick = (now: number) => {
      const frameMs = 1000 / playRef.current.speed;
      acc += now - last;
      last = now;
      const steps = Math.floor(acc / frameMs);
      if (steps > 0) {
        acc -= steps * frameMs;
        // Advance sub-candle by sub-candle so the bar GROWS; roll to the next
        // display candle only when its last sub-step is consumed.
        setSub((prevSub) => {
          let i = indexRef.current;
          let k = prevSub + steps;
          const f = fetchedRef.current;
          for (;;) {
            const n = f ? subCount(f, i + 1) : 1;
            if (k < n || i >= totalRef.current - 1) break;
            k -= n;
            i += 1;
          }
          if (i !== indexRef.current) setIndex(i);
          if (i >= totalRef.current - 1) {
            setPlaying(false);
            return 0;
          }
          return k;
        });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const holidays = useMemo(
    () => (draft.global.holidays ?? []).map((h) => h.date),
    [draft.global.holidays],
  );
  const startReplay = () => {
    const d = replayDateInput || fetched?.session_date || histDate;
    if (!d) return;
    const wd = new Date(`${d}T00:00:00Z`).getUTCDay();
    if (wd === 0 || wd === 6) {
      setError("Replay: pick a weekday");
      return;
    }
    setPlaying(false);
    seekToRef.current = `${d}T${replayTimeInput || "09:15"}:00`;
    setReplay({ date: d });
  };
  const exitReplay = () => {
    setPlaying(false);
    setReplay(null);
    setIndex(0);
  };
  const stepDay = (delta: 1 | -1) => {
    if (!replay) return;
    const next = stepTradingDay(
      replay.date, delta, holidays,
      fetched?.first_session_date ?? null, fetched?.expiry ?? null,
    );
    if (!next) return;
    setPlaying(false);
    seekToRef.current = `${next}T09:15:00`;
    setReplay({ date: next });
  };
  const onIntervalChange = (v: UmpDisplayInterval) => {
    if (replay && fetched?.candles?.length) {
      // Re-map the playhead by timestamp after the refetch.
      const cur = fetched.candles[Math.min(index, fetched.candles.length - 1)];
      seekToRef.current = replayCursorTs ?? cur.ts;
      setPlaying(false);
    }
    setInterval_(v);
  };

  const set = useCallback(
    (fn: (p: UmpParams) => void) => {
      mutate((doc) => {
        const p = doc.days[day]?.zones[zone]?.ump;
        if (p) fn(p);
      });
    },
    [mutate, day, zone]
  );

  if (!params) return <div className="text-sm text-muted">No zone configuration.</div>;

  return (
    <div className="flex flex-col gap-4">
      <EngineActions
        dirty={dirty}
        day={day}
        zone={zone}
        canReset={defaults !== null}
        onRequestSave={onRequestSave}
        onCopyTo={onCopyTo}
        onRerun={() => void load()}
        loading={loading}
        onResetDefaults={() => {
          const src = defaults?.days[day]?.zones[zone]?.ump;
          if (src) {
            mutate((d) => void (d.days[day].zones[zone].ump = JSON.parse(JSON.stringify(src))));
          }
        }}
      />
      {dirty && (
        <CalcNote tone="warn">
          Unsaved engine parameters — the evaluation below reflects the <b>saved</b> config.
          Save to apply your edits.
        </CalcNote>
      )}
      {error && <div className="text-xs text-ce">{error}</div>}

      <CalcNote>
        Every calculation on this page is conformance-audited line-by-line against the
        Pine source (zero divergences) — the script itself lives at
        <b> docs/reference/nifty_ultra_master_pro_v20.pine</b> and the full
        section-by-section proof at <b>docs/ump-pine-conformance.md</b>.
      </CalcNote>

      {/* Data-Ready Gate strip */}
      <div className="panel px-4 py-2.5 flex items-center gap-4 flex-wrap text-[11px]">
        <b className="text-accent">Data-Ready Gate:</b>
        {data ? (
          <>
            <GateDot ok={(data.entry_candles?.length ?? 0) > 0} label={`${data.entry_timeframe_min}m feed`} />
            <GateDot ok={data.gate.daily_feeds >= 3} label={`Daily feed (${data.gate.daily_feeds}/3)`} />
            <GateDot ok={data.gate.weekly_feed} label="Weekly feed" />
            <GateDot ok={data.gate.h1_candles > 0} label={`1H feed (${data.gate.h1_candles})`} />
            <div className="flex-1" />
            <span className={data.gate.levels_ready ? "text-pe font-bold" : "text-amber-400 font-bold"}>
              {data.gate.levels_ready
                ? "GATE OPEN — entries permitted"
                : "GATE CLOSED — entries blocked until all feeds resolve"}
            </span>
            <span className="text-muted flex items-center gap-1 flex-wrap">
              {data.symbol}
              <select
                className="bg-panel border border-border rounded px-1 py-0.5 text-[11px]"
                title="Which contract to evaluate. 'auto' = the zone's premium-band pick — the strike the orchestrator would actually hunt. HUNTED lists its parallel multi-strike set (inside the premium band, nearest band-mid first); ALL STRIKES lets you inspect any contract on the chain manually."
                value={strikeOverride == null ? "auto" : String(strikeOverride)}
                onChange={(e) =>
                  setStrikeOverride(e.target.value === "auto" ? null : Number(e.target.value))
                }
              >
                <option value="auto">
                  {strikeOverride == null ? `${data.strike} (auto)` : "auto (band pick)"}
                </option>
                {(data.band_candidates ?? []).length > 0 && (
                  <optgroup label={`HUNTED — in band (${(data.band_candidates ?? []).length})`}>
                    {(data.band_candidates ?? []).map((c) => (
                      <option key={`b${c.strike}`} value={String(c.strike)}>
                        ● {c.strike} @ ₹{c.premium.toFixed(2)}
                      </option>
                    ))}
                  </optgroup>
                )}
                {(data.all_strikes ?? []).length > 0 && (
                  <optgroup label={`ALL STRIKES (${(data.all_strikes ?? []).length})`}>
                    {(data.all_strikes ?? []).map((c) => (
                      <option key={`a${c.strike}`} value={String(c.strike)}>
                        {(data.band_candidates ?? []).some((b) => b.strike === c.strike) ? "● " : ""}
                        {c.strike} @ ₹{c.premium.toFixed(2)}
                      </option>
                    ))}
                  </optgroup>
                )}
                {strikeOverride != null &&
                  !(data.all_strikes ?? []).some((c) => c.strike === strikeOverride) &&
                  !(data.band_candidates ?? []).some((c) => c.strike === strikeOverride) && (
                    <option value={String(strikeOverride)}>{strikeOverride}</option>
                  )}
              </select>
              <span
                className={`px-1 rounded text-[9.5px] font-bold ${
                  data.strike_source === "band"
                    ? "bg-pe/15 text-pe"
                    : data.strike_source === "manual"
                      ? "bg-accent/15 text-accent"
                      : data.strike_source === "position"
                        ? "bg-sky-500/15 text-sky-300"
                        : "bg-amber-500/15 text-amber-300"
                }`}
                title={
                  data.strike_source === "band"
                    ? "Strike = the zone's premium-band pick (what the orchestrator trades)"
                    : data.strike_source === "manual"
                      ? "Strike chosen manually"
                      : data.strike_source === "position"
                        ? "Strike = the contract the engine is trading right now — the chart stays on it until the trade closes"
                        : "No strike currently sits inside this zone's premium band — showing the ATM contract as a fallback"
                }
              >
                {data.strike_source === "band"
                  ? "BAND"
                  : data.strike_source === "manual"
                    ? "MANUAL"
                    : data.strike_source === "position"
                      ? "IN TRADE"
                      : "ATM fallback"}
              </span>
              {data.candidates_as_of && (
                <span
                  className="text-[9px] text-muted"
                  title="The moment these candidate premiums are from. Off-hours the ladder freezes at the chain's last stored trade."
                >
                  as of {data.candidates_as_of.slice(11, 16)}
                </span>
              )}
              <select
                className="bg-panel border border-border rounded px-1 py-0.5 text-[11px]"
                value={optionType}
                onChange={(e) => setOptionType(e.target.value as "CE" | "PE")}
              >
                <option>CE</option>
                <option>PE</option>
              </select>{" "}
              · exp {data.expiry} · {data.first_session_date} → {data.session_date}
            </span>
          </>
        ) : (
          <span className="text-muted">Loading…</span>
        )}
      </div>

      {/* Replay bar (§4) — TradingView-style: pick an instant, play forward. */}
      <div className="panel px-4 py-2 flex items-center gap-2 flex-wrap text-[11px]">
        {!replayOpen && !replay ? (
          <button
            type="button"
            className="pill text-[11px]"
            title="Replay the chart bar by bar from a chosen date/time — entries, exits, trail labels and the level ladder appear only with the data known at that instant"
            onClick={() => {
              setReplayDateInput(histDate || fetched?.session_date || "");
              setReplayOpen(true);
            }}
          >
            ▶ Replay
          </button>
        ) : (
          <>
            <b className="text-accent">Replay</b>
            {!replay && (
              <>
                <span className="text-muted">from</span>
                <input
                  type="date"
                  className="bg-panel border border-border rounded px-1 py-0.5 text-[11px]"
                  value={replayDateInput}
                  max={fetched?.expiry}
                  onChange={(e) => setReplayDateInput(e.target.value)}
                />
                <input
                  type="time"
                  className="bg-panel border border-border rounded px-1 py-0.5 text-[11px]"
                  value={replayTimeInput}
                  onChange={(e) => setReplayTimeInput(e.target.value)}
                />
                <button type="button" className="pill pill-active text-[11px]" onClick={startReplay}>
                  Start
                </button>
                <button type="button" className="pill text-[11px]" onClick={() => setReplayOpen(false)}>
                  Close
                </button>
              </>
            )}
            {replay && (
              <>
                <button type="button" className="pill text-[11px]" onClick={() => stepDay(-1)} title="Previous trading day">
                  ◀ prev day
                </button>
                <span className="font-mono text-gray-100">{replay.date}</span>
                <button type="button" className="pill text-[11px]" onClick={() => stepDay(1)} title="Next trading day (capped at expiry)">
                  next day ▶
                </button>
                <span className="text-[9.5px] text-muted">
                  candles = {fetched?.candles_interval && fetched.candles_interval < 60
                    ? `${fetched.candles_interval}s`
                    : `${(fetched?.candles_interval ?? 300) / 60}m`} · speeds are bars/second ·
                  expiry-day 15:25 force-close is an orchestrator rule (not shown here)
                </span>
                <button type="button" className="pill text-[11px] ml-auto" onClick={exitReplay}>
                  ✕ Exit replay
                </button>
              </>
            )}
          </>
        )}
        {replay && !replayReady && (
          <div className="w-full text-[11px] text-muted">
            Loading the replay… the controls appear once the data for {replay.date} has arrived.
          </div>
        )}
        {replay && fetched && replayReady && (
          <div className="w-full">
            <ReplayController
              playing={playing}
              index={index}
              total={fetched.candles?.length ?? 0}
              speed={speed}
              currentTs={replayCursorTs}
              onPlayPause={() => {
                if (!playing && index >= (fetched.candles?.length ?? 1) - 1) {
                  setIndex(indexAtOrAfter(fetched.candles ?? [], `${replay.date}T09:15:00`));
                }
                setPlaying((p) => !p);
              }}
              onRestart={() => {
                setSub(0);
                setPlaying(false);
                setIndex(indexAtOrAfter(fetched.candles ?? [], `${replay.date}T09:15:00`));
              }}
              onSeek={(i) => {
                setIndex(Math.max(0, Math.min(i, (fetched.candles?.length ?? 1) - 1)));
                setSub(0);
              }}
              onSkip={(d) => skipSub(d)}
              onSpeed={setSpeed}
            />
          </div>
        )}
      </div>

      {liveCandle && (
        <div
          className={`text-[10.5px] px-1 ${liveCandle.on ? "text-pe" : "text-amber-300"}`}
          title="The moving (forming) candle comes from the live stream; closed candles come from the 30-second refresh."
        >
          {liveCandle.on ? "● " : "▲ "}
          {liveCandle.text}
        </div>
      )}
      {/* Chart with levels + events */}
      {data && (
        <UmpTvChart
          data={data}
          params={params}
          // During replay the forming bar comes from the sub-candles, through the
          // SAME incremental update path the live one uses.
          liveBar={replay ? replayBar : liveBar}
          interval={interval}
          onIntervalChange={onIntervalChange}
          replaying={!!replay}
        />
      )}

      {(() => {
        // ── Pine dashboard + key-levels panels, honouring Show toggles and
        // the Position selectors (TV corners → column + order slots). ──
        const lastClose = data?.candles?.length
          ? data.candles[data.candles.length - 1].c
          : null;
        const chgPct =
          lastClose != null && data?.prev_close != null && data.prev_close !== 0
            ? ((lastClose - data.prev_close) / data.prev_close) * 100
            : null;
        const medColor = /^#[0-9a-fA-F]{6}$/.test(params.visual.median_color)
          ? params.visual.median_color
          : "#FFD700";
        const typeColor = (t: number) => (t === 4 ? medColor : TYPE_COLOR[t]);
        const TYPE_ICON: Record<number, string> = { 1: "◆", 2: "▲", 3: "◇", 4: "✦" };

        const dashboardCard = params.dashboard.show_dashboard ? (
          <Card key="dash" title="◆ Ultra-Master Dashboard" hint="Pine's dashboard table — live engine readout">
            {(
              [
                ["Symbol", data ? `${data.symbol} ${data.strike} ${data.option_type}` : "—", "text-gray-100"],
                [
                  "💰 Price",
                  lastClose != null
                    ? `₹${lastClose.toFixed(2)}${chgPct != null ? `  ${chgPct >= 0 ? "+" : ""}${chgPct.toFixed(2)}% vs prev close` : ""}`
                    : "—",
                  chgPct == null || chgPct >= 0 ? "text-pe" : "text-ce",
                ],
                [
                  "🔴 Resistance",
                  data?.nearest_resistance
                    ? `₹${data.nearest_resistance.price.toFixed(2)} (+${data.nearest_resistance.distance_pct.toFixed(2)}%)`
                    : "—",
                  "text-ce",
                ],
                [
                  "🟢 Support",
                  data?.nearest_support
                    ? `₹${data.nearest_support.price.toFixed(2)} (−${data.nearest_support.distance_pct.toFixed(2)}%)`
                    : "—",
                  "text-pe",
                ],
                ["🗺️ Levels", data ? `${data.levels.length} mapped` : "—", "text-accent"],
                [
                  "📐 Entry Model",
                  data?.state === "IN_TRADE"
                    ? `🟢 ACTIVE — ${data.sub_scenario}`
                    : data?.state === "WATCHING"
                      ? "🟡 WATCHING…"
                      : "⚪ No Signal",
                  data?.state === "IN_TRADE" ? "text-pe" : data?.state === "WATCHING" ? "text-amber-400" : "text-muted",
                ],
                [
                  "Entry / SL",
                  data?.state === "IN_TRADE" && data.entry_price != null
                    ? `₹${data.entry_price.toFixed(2)}  SL: ₹${data.base_level?.toFixed(2) ?? "—"}`
                    : "—",
                  "text-gray-100",
                ],
              ] as const
            ).map(([label, value, cls]) => (
              <div key={label} className="flex justify-between text-xs py-1 border-b border-border/30 last:border-b-0">
                <span className="text-muted">{label}</span>
                <span className={`font-semibold ${cls}`}>{value}</span>
              </div>
            ))}
          </Card>
        ) : null;

        const rows = (data?.levels ?? [])
          .map((l) => ({
            ...l,
            dist: lastClose != null && lastClose > 0 ? ((l.price - lastClose) / lastClose) * 100 : null,
          }))
          .sort((a, b) => b.price - a.price);
        const shown = nearestOnly && lastClose != null
          ? rows
              .slice()
              .sort((a, b) => Math.abs(a.dist ?? 0) - Math.abs(b.dist ?? 0))
              .slice(0, 7)
              .sort((a, b) => b.price - a.price)
          : rows;
        const levelsCard = params.dashboard.show_key_levels ? (
          <Card key="lvls" title="🗺️ Key Levels" hint="price + signed distance from the live premium">
            <div className="flex gap-2 flex-wrap mb-2 items-center">
              {[1, 2, 3, 4].map((t) => {
                const n = data?.levels.filter((l) => l.type === t).length ?? 0;
                return (
                  <span
                    key={t}
                    className="text-[10px] font-bold px-2 py-0.5 rounded-full border border-border"
                    style={{ color: typeColor(t) }}
                  >
                    {n} {["", "Structural", "Discovery", "Bridge", "Median"][t]}
                  </span>
                );
              })}
              {data?.state === "IN_TRADE" && (
                <span className="text-[10px] font-bold text-ce">FROZEN — trade active</span>
              )}
              <button
                type="button"
                className="pill text-[10px] ml-auto"
                onClick={() => setNearestOnly((v) => !v)}
              >
                {nearestOnly ? "7 nearest · show all" : "all levels · show 7 nearest"}
              </button>
            </div>
            <div className="max-h-56 overflow-y-auto">
              {shown.map((l) => (
                <div
                  key={`${l.price}-${l.type}`}
                  className="grid grid-cols-[1fr_auto_64px] gap-2 text-xs py-1 border-b border-border/30 last:border-b-0"
                >
                  <span style={{ color: typeColor(l.type) }}>
                    {TYPE_ICON[l.type]} {l.name}
                  </span>
                  <span className="font-mono text-gray-100">₹{l.price.toFixed(2)}</span>
                  <span
                    className={`font-mono text-right ${
                      l.dist == null ? "text-muted" : l.dist > 0 ? "text-ce" : "text-pe"
                    }`}
                  >
                    {l.dist != null ? `${l.dist >= 0 ? "+" : ""}${l.dist.toFixed(2)}%` : "—"}
                  </span>
                </div>
              ))}
              {shown.length === 0 &&
            (loading ? <LoadingBlock /> : <div className="text-xs text-muted">No levels yet.</div>)}
            </div>
          </Card>
        ) : null;

        const colOf = (pos: string) => (pos.includes("Left") ? "left" : "right");
        const slotOf = (pos: string) => (pos.startsWith("Top") ? 0 : pos.startsWith("Middle") ? 1 : 2);
        const placed = (col: "left" | "right", slot: 0 | 1 | 2) => (
          <>
            {dashboardCard != null &&
              colOf(params.dashboard.dashboard_position) === col &&
              slotOf(params.dashboard.dashboard_position) === slot &&
              dashboardCard}
            {levelsCard != null &&
              colOf(params.dashboard.levels_position) === col &&
              slotOf(params.dashboard.levels_position) === slot &&
              levelsCard}
          </>
        );
        return (
      <div className="grid lg:grid-cols-[1.4fr_1fr] gap-4">
        <div className="flex flex-col gap-4">
          {placed("left", 0)}
          {/* State machine */}
          <Card title="Entry Model State Machine" hint="idle → calculating/watching → in trade">
            <div
              className={`text-center rounded-lg py-3 mb-3 border ${
                data?.state === "IN_TRADE"
                  ? "border-pe/40 bg-pe/5"
                  : data?.state === "WATCHING"
                    ? "border-amber-700/40 bg-amber-950/20"
                    : "border-border bg-panel"
              }`}
            >
              <div className="text-[10px] text-muted uppercase">Current State</div>
              <div
                className={`text-xl font-extrabold ${
                  data?.state === "IN_TRADE"
                    ? "text-pe"
                    : data?.state === "WATCHING"
                      ? "text-amber-400"
                      : "text-gray-300"
                }`}
              >
                {data?.state === "IN_TRADE" ? "IN TRADE" : data?.state ?? "—"}
              </div>
              {data?.state === "IN_TRADE" && (
                <div className="text-[11px] text-muted mt-1">
                  Sub-Scenario: <b className="text-gray-100">{data.sub_scenario}</b> · Entry:{" "}
                  <b className="text-gray-100">{data.entry_price?.toFixed(2)}</b> · Base:{" "}
                  <b className="text-gray-100">{data.base_level?.toFixed(2)}</b>
                </div>
              )}
            </div>
            <CalcNote>
              Retest entries (R1/R2) fire only on Structural, Discovery & Bridge levels —
              Median is excluded (synthetic, lower confidence). The filter layer opens the
              door; only price action (or the day's End-Exit) closes it.
            </CalcNote>
          </Card>
          {placed("left", 1)}

          {/* Exit & trail */}
          <Card title="Exit & Trail System" hint="priority order — first to fire wins">
            {/* `pending` while the replay is still running. Without it these rows
                asserted "idle", "not armed" and "arms near NB" for the several
                seconds the load takes — readings the engine had not made yet. */}
            <ExitRow n="P1" name="Max SL (Hard Cap)" desc="Intrabar · both models"
              value={data?.max_sl != null ? data.max_sl.toFixed(2) : "—"}
              status={data?.state === "IN_TRADE" ? "always armed" : "idle"} pending={waiting} />
            <ExitRow n="P2" name="Trail Exit (System A)" desc="Intrabar touch · both models"
              value={data?.trail_sl != null ? data.trail_sl.toFixed(2) : "—"}
              status={data?.trail_sl != null ? "ARMED" : "not armed"} armed={data?.trail_sl != null}
              pending={waiting} />
            <ExitRow n="P2b" name="Zone Trail (System B)" desc="Body Closing only"
              value={data?.sb_trail_sl != null ? data.sb_trail_sl.toFixed(2) : "arms near NB"}
              status={data?.sb_stage ? `stage ${data.sb_stage}` : "not armed"} armed={(data?.sb_stage ?? 0) > 0}
              pending={waiting} />
            <ExitRow n="P3" name="Target Hit" desc="Intrabar · Body Closing only"
              value={data?.q_levels ? data.q_levels.nb.toFixed(2) : "—"}
              status={data?.state === "IN_TRADE" ? "watching" : "idle"} pending={waiting} />
            <ExitRow n="P4" name="Base SL" desc="Confirmed close only"
              value={data?.base_level != null ? data.base_level.toFixed(2) : "—"}
              status={data?.trail_sl != null ? "disabled — trail above Base" : "armed while no trail"}
              pending={waiting} />
            {data?.q_levels && (
              <>
                <div className="border-t border-border/40 mt-2 pt-2 text-[11px] text-muted mb-1">
                  System A Quarter Rungs (Base {data.base_level?.toFixed(2)} → NB{" "}
                  {data.q_levels.nb.toFixed(2)}):
                </div>
                {(["q1", "q2", "q3"] as const).map((q) => (
                  <div
                    key={q}
                    className={`flex justify-between text-xs px-2.5 py-1 rounded border mb-1 ${
                      data.trail_sl != null && Math.abs(data.trail_sl - data.q_levels![q]) < 1e-6
                        ? "border-amber-600/50 bg-amber-950/20 text-amber-300"
                        : "border-border bg-panel text-gray-300"
                    }`}
                  >
                    <span>{q.toUpperCase()} — {data.q_levels![q].toFixed(2)}</span>
                    <span>
                      {data.trail_sl != null && Math.abs(data.trail_sl - data.q_levels![q]) < 1e-6
                        ? "current trail"
                        : ""}
                    </span>
                  </div>
                ))}
              </>
            )}
          </Card>
          {placed("left", 2)}
        </div>

        <div className="flex flex-col gap-4">
          {placed("right", 0)}
          {/* Zone sub-levels */}
          {data?.active_zone && (
            <Card title="Zone Sub-Levels" hint="the active trade's Base zone">
              {(
                [
                  ["Zone Top (ZT)", data.active_zone.zone_top],
                  ["Upper Median (UM)", data.active_zone.upper_median],
                  ["Base (B)", data.active_zone.base],
                  ["Lower Median (LM)", data.active_zone.lower_median],
                  ["Zone Bottom (ZB)", data.active_zone.zone_bottom],
                ] as const
              ).map(([label, v]) => (
                <div key={label} className="flex justify-between text-xs py-1 border-b border-border/30 last:border-b-0">
                  <span className="text-muted">{label}</span>
                  <span className={`font-mono ${label.startsWith("Base") ? "text-white font-bold" : "text-gray-200"}`}>
                    {v.toFixed(2)}
                  </span>
                </div>
              ))}
            </Card>
          )}
          {placed("right", 1)}

          {/* Trade history */}
          <Card title="Trade History Log">
            <div className="max-h-56 overflow-y-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-muted text-left">
                    <th className="py-1 font-medium">Entry</th>
                    <th className="py-1 font-medium">Exit</th>
                    <th className="py-1 font-medium">Reason</th>
                    <th className="py-1 font-medium text-right">P&L (pts)</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.trades.slice().reverse().map((t, i) => (
                    <tr key={`${t.entry_ts}-${i}`} className="border-t border-border/30">
                      <td className="py-1">
                        {t.sub_scenario} @ {t.entry_price.toFixed(2)}
                      </td>
                      <td className="py-1">{t.exit_price != null ? t.exit_price.toFixed(2) : "open"}</td>
                      <td className="py-1 text-muted" title={t.exit_reason || undefined}>
                        {exitReasonLabel(t.exit_reason)}
                      </td>
                      <td
                        className={`py-1 text-right font-bold ${
                          (t.pnl_points ?? 0) >= 0 ? "text-pe" : "text-ce"
                        }`}
                      >
                        {t.pnl_points != null ? (t.pnl_points >= 0 ? "+" : "") + t.pnl_points.toFixed(2) : "—"}
                      </td>
                    </tr>
                  )) ?? null}
                  {(data?.trades.length ?? 0) === 0 && (
                    <tr>
                      <td colSpan={4} className="py-2 text-muted">
                    {loading
                      ? <LoadingBlock label="Replaying the contract's stored life…" />
                      : "No trades in the contract's stored life."}
                  </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
          {placed("right", 2)}
        </div>
      </div>
        );
      })()}

      {/* Zone settings — the COMPLETE Pine Inputs tab: all 26 inputs, the
          same 5 groups, exactly like TradingView's settings dialog. */}
      <Card
        title={`Zone Settings — ${day.charAt(0).toUpperCase() + day.slice(1)} ${zone}`}
        hint="all 26 Pine inputs + entry timeframe & per-kind switches, TradingView-style · numeric fields carry no min/max — extreme values are trading decisions"
      >
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-3">
          <div>
            <div className="text-[11px] font-bold text-gray-200 mb-2">🏛️ Institutional Master Laws</div>
            <div className="grid grid-cols-2 gap-2">
              <NumField label="Expansion Law (20% Spacing)" value={params.institutional.expansion_law_pct} onChange={(v) => set((p) => void (p.institutional.expansion_law_pct = v))} />
              <NumField label="Bridge Law (50% Void)" value={params.institutional.bridge_law_pct} onChange={(v) => set((p) => void (p.institutional.bridge_law_pct = v))} />
              <NumField label="Median Law (Gap %)" value={params.institutional.median_law_pct} onChange={(v) => set((p) => void (p.institutional.median_law_pct = v))} />
              <NumField label="Price Floor (₹ Stop)" value={params.institutional.price_floor_inr} onChange={(v) => set((p) => void (p.institutional.price_floor_inr = v))} />
            </div>
            <div className="text-[11px] font-bold text-gray-200 mb-2 mt-3">🔍 Structural Detection</div>
            <div className="grid grid-cols-2 gap-2">
              <NumField label="Body Match (pts)" value={params.structural.body_match_pts} step={0.1} onChange={(v) => set((p) => void (p.structural.body_match_pts = v))} />
              <NumField label="1H Scan Depth (Bars)" value={params.structural.scan_depth_bars} onChange={(v) => set((p) => void (p.structural.scan_depth_bars = Math.round(v)))} />
            </div>
          </div>
          <div>
            <div className="text-[11px] font-bold text-gray-200 mb-2">📐 Entry Model</div>
            <div className="grid grid-cols-2 gap-2">
              <SelectField
                label="Trade Entry Timeframe"
                value={String(params.entry.entry_timeframe_min ?? 5)}
                options={[
                  { value: "5", label: "5-Minute" },
                  { value: "15", label: "15-Minute" },
                ]}
                onChange={(v) => set((p) => void (p.entry.entry_timeframe_min = (Number(v) === 15 ? 15 : 5)))}
              />
              <NumField label="Max SL % (Hard Cap)" value={params.entry.max_sl_pct} step={0.1} onChange={(v) => set((p) => void (p.entry.max_sl_pct = v))} />
              <NumField label={`Trigger Timeout (bars × ${params.entry.entry_timeframe_min ?? 5}m)`} value={params.entry.trigger_timeout_bars} onChange={(v) => set((p) => void (p.entry.trigger_timeout_bars = Math.round(v)))} />
            </div>
            <div className="text-[9.5px] text-muted mt-1">
              The entry timeframe changes ONLY the candle the trigger / test-candle / retest
              logic evaluates. Key levels (daily · weekly · 1H structure) are unchanged.
            </div>
            {(
              [
                ["Show Entry Signals", "show_entry_signals"],
                ["Enable LONG entries (▲)", "enable_long"],
                ["Enable RETEST entries (▲R)", "enable_retest"],
                ["Show SL Lines", "show_sl_lines"],
                ["Show Trail Labels", "show_trail_labels"],
              ] as const
            ).map(([label, key]) => (
              <div key={key} className="flex items-center justify-between mt-1.5 text-xs">
                <span className="text-muted">{label}</span>
                <Switch
                  on={params.entry[key]}
                  onChange={(v) => set((p) => void (p.entry[key] = v))}
                />
              </div>
            ))}
            {/* Per-entry-kind switches (§2) under the two master switches. */}
            {(
              [
                ["Body-Closing entries", ["S1A", "S1B", "S1C", "S2A", "S2B", "S3A", "S3B", "S3C"], params.entry.enable_long],
                ["Retest entries", ["R1", "R2"], params.entry.enable_retest],
              ] as const
            ).map(([title, kinds, masterOn]) => (
              <div key={title} className="mt-2">
                <div className="text-[10px] font-semibold text-gray-300 mb-1 flex items-center gap-2">
                  {title}
                  {!masterOn && (
                    <span className="text-[9px] text-amber-300 font-normal">master switch OFF — none of these can fire</span>
                  )}
                </div>
                <div className={`grid grid-cols-4 gap-1 ${masterOn ? "" : "opacity-50"}`}>
                  {kinds.map((k) => {
                    const on = params.entry.scenarios?.[k] ?? true;
                    return (
                      <button
                        key={k}
                        type="button"
                        title={`${k} — ${on ? "ON" : "OFF"}. A disabled kind is refused at the entry commit; the trigger stays armed for a later candle.`}
                        className={`text-[10px] font-bold rounded border px-1 py-0.5 ${
                          on ? "border-border" : "border-border/40 line-through text-muted"
                        }`}
                        style={on ? { color: ENTRY_STYLE[k]?.color } : undefined}
                        onClick={() =>
                          set((p) => {
                            const sc = { ...(p.entry.scenarios ?? {}) };
                            for (const kk of UMP_SCENARIOS) if (!(kk in sc)) sc[kk] = true;
                            sc[k] = !on;
                            p.entry.scenarios = sc;
                          })
                        }
                      >
                        {k}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
            <div className="text-[11px] font-bold text-gray-200 mb-2 mt-3">📊 Dashboard</div>
            {(
              [
                ["Show Dashboard", "show_dashboard"],
                ["Show Key Levels", "show_key_levels"],
              ] as const
            ).map(([label, key]) => (
              <div key={key} className="flex items-center justify-between mt-1.5 text-xs">
                <span className="text-muted">{label}</span>
                <Switch
                  on={params.dashboard[key]}
                  onChange={(v) => set((p) => void (p.dashboard[key] = v))}
                />
              </div>
            ))}
            <div className="grid grid-cols-2 gap-2 mt-2">
              <SelectField
                label="Dashboard Position"
                value={params.dashboard.dashboard_position}
                options={["Top Right", "Top Left", "Bottom Right", "Bottom Left", "Middle Right"].map((v) => ({ value: v, label: v }))}
                onChange={(v) => set((p) => void (p.dashboard.dashboard_position = v))}
              />
              <SelectField
                label="Levels Position"
                value={params.dashboard.levels_position}
                options={["Top Right", "Top Left", "Bottom Right", "Bottom Left", "Middle Left"].map((v) => ({ value: v, label: v }))}
                onChange={(v) => set((p) => void (p.dashboard.levels_position = v))}
              />
            </div>
          </div>
          <div>
            <div className="text-[11px] font-bold text-gray-200 mb-2">🎨 Visual</div>
            <div className="grid grid-cols-2 gap-2">
              <NumField label="Zone Width ± %" value={params.visual.zone_width_pct} step={0.1} onChange={(v) => set((p) => void (p.visual.zone_width_pct = v))} />
              <NumField label="Median Line Width" value={params.visual.median_line_width} onChange={(v) => set((p) => void (p.visual.median_line_width = Math.round(v)))} />
              <NumField label="Line Left Extension (Bars)" value={params.visual.line_extension_bars} onChange={(v) => set((p) => void (p.visual.line_extension_bars = Math.round(v)))} />
              <NumField label="Label Right Offset (Bars)" value={params.visual.label_right_offset_bars} onChange={(v) => set((p) => void (p.visual.label_right_offset_bars = Math.round(v)))} />
            </div>
            <div className="mt-2">
              <ColorField
                label="Median Color"
                value={params.visual.median_color}
                onChange={(v) => set((p) => void (p.visual.median_color = v))}
              />
            </div>
            {(
              [
                ["Show 1H-STRUCT (Red)", "show_struct"],
                ["Show DISCOVERY (Green)", "show_discovery"],
                ["Show BRIDGE (Cyan)", "show_bridge"],
                ["Show MEDIAN (Yellow)", "show_median"],
              ] as const
            ).map(([label, key]) => (
              <div key={key} className="flex items-center justify-between mt-1.5 text-xs">
                <span className="text-muted">{label}</span>
                <Switch
                  on={params.visual[key]}
                  onChange={(v) => set((p) => void (p.visual[key] = v))}
                />
              </div>
            ))}
            <div className="text-[9.5px] text-muted mt-2">
              Line extension / label offset are TradingView-canvas concepts — kept for
              exact Pine parity; the chart engine here draws full-width labelled lines.
              Every other visual setting takes effect on the chart instantly after Save.
            </div>
          </div>
        </div>
      </Card>
    </div>
  );
}

function GateDot(props: { ok: boolean; label: string }) {
  return (
    <span className={`flex items-center gap-1.5 font-semibold ${props.ok ? "text-pe" : "text-ce"}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${props.ok ? "bg-pe" : "bg-ce"}`} />
      {props.label}
    </span>
  );
}

function ExitRow(props: {
  n: string;
  name: string;
  desc: string;
  value: string;
  status: string;
  armed?: boolean;
  /** No evaluation yet — show that rather than a status the engine has not
   *  produced. */
  pending?: boolean;
}) {
  return (
    <div className="grid grid-cols-[40px_1fr_100px_90px] gap-2 items-center py-1.5 border-b border-border/30 last:border-b-0">
      <span className="text-center text-[10px] font-extrabold text-white bg-accent rounded px-1 py-0.5">
        {props.n}
      </span>
      <div>
        <div className="text-xs font-semibold text-gray-100">{props.name}</div>
        <div className="text-[9.5px] text-muted">{props.desc}</div>
      </div>
      <span className={`text-[10px] font-bold ${props.armed ? "text-amber-400" : "text-muted"}`}>
        {props.pending ? (
          <span className="inline-flex items-center gap-1 text-muted font-normal">
            <Spinner size={8} />
            evaluating…
          </span>
        ) : (
          props.status
        )}
      </span>
      <span className="text-right text-xs font-bold text-gray-100">
        {props.pending ? "" : props.value}
      </span>
    </div>
  );
}

