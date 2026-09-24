/**
 * Sub-tab 1 — Daily Trading Configuration.
 *
 * Everything for one day together (reference: algo_config_full.html §1): the
 * master kill bar, the 20-switch kill grid, account & sizing, then per-day
 * zone rows + risk & alerts + the per-zone indicator enable table with the
 * automatic unanimous-among-enabled rule note.
 *
 * End-Exit is a DAY-level rule keyed to whichever zone is last by End time
 * (§2.3) — earlier zones render N/A, and the toggle follows the clock if zone
 * times are edited, not a fixed Z3 label.
 */
import { useCallback, useEffect, useState } from "react";
import { algoApi } from "../../api/algoRest";
import type {
  AlgoConfigDoc,
  DayConfig,
  OrchestratorStatus,
  Weekday,
  ZoneConfig,
  ZoneId,
} from "../../types/algo";
import { INDICATOR_LABEL, WEEKDAY_LABEL, WEEKDAYS, ZONE_IDS } from "../../types/algo";
import { CalcNote, Card, NumField, SelectField, Switch, TimeField, ToggleLine } from "./controls";
import { LiveDecisionTrace } from "./DecisionTrace";
import { CopySettingsDialog } from "./CopySettingsDialog";
import type { CopyEngine, CopyScope } from "./copySettings";
import { applyCopy, copyZoneInto, deepClone } from "./copySettings";
import { trackRequest } from "../../api/inflight";

/** Indicator-row slot → the engine section it configures. */
const IND_ENGINE: Record<keyof typeof INDICATOR_LABEL, CopyEngine> = {
  oi_change: "oi_structure",
  multi_tf: "mtf_ratio",
  ratio: "mqae",
};

const INDEX_OPTIONS = ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"].map((v) => ({
  value: v,
  label: v,
}));

function hhmmToMin(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}

function lastZoneId(day: DayConfig): ZoneId {
  let best: ZoneId = "Z1";
  let bestEnd = -1;
  for (const z of ZONE_IDS) {
    const end = hhmmToMin(day.zones[z]?.end ?? "00:00");
    if (end > bestEnd) {
      best = z;
      bestEnd = end;
    }
  }
  return best;
}

function fmtRupee(n: number): string {
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  todayWeekday: Weekday | null;
  /** false in the Backtesting workspace: no live orchestrator poll/Resume. */
  showLiveSnapshot?: boolean;
  /** Jump to an engine dashboard with this zone's context ("ump" = Ultra
   *  Master Pro). Indicator names in the zone rows are links to it. */
  onOpenEngine?: (
    ind: keyof typeof INDICATOR_LABEL | "ump",
    day: Weekday,
    zone: ZoneId
  ) => void;
  /** Appendix-A reference document — the source for the Reset actions. */
  defaults?: AlgoConfigDoc | null;
}

const WD_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Zone "Strikes" ceiling = one chain side at the widest data window (±60).
 *  Mirrors ZoneConfig.strike_scan_count's le=121 (was a hard 10 until
 *  2026-09-23). */
const STRIKE_SCAN_MAX = 121;

function expiryWithDay(iso: string): string {
  const dt = new Date(`${iso}T00:00:00`);
  return Number.isNaN(dt.getTime()) ? iso : `${iso} · ${WD_SHORT[dt.getDay()]}`;
}

// deepClone / copyZoneInto live in ./copySettings (shared with the dialog).

export function DailyConfigPanel({ draft, mutate, todayWeekday, onOpenEngine, defaults, showLiveSnapshot = true }: Props) {
  const [day, setDay] = useState<Weekday>(todayWeekday ?? "monday");
  const [expiries, setExpiries] = useState<string[]>([]);
  /** §9 copy dialog — null = closed; the scope seeds its source selectors. */
  const [copyScope, setCopyScope] = useState<CopyScope | null>(null);
  const d = draft.days[day];
  const g = draft.global;
  const lastZ = lastZoneId(d);

  // The strip must follow the SELECTED DAY's index, not a fixed one: a SENSEX
  // day settles on Thursdays and a NIFTY day on Tuesdays, so a hardcoded symbol
  // showed the wrong contract entirely. The engine-tabs dropdown had the same
  // bug and was fixed on 2026-08-18 (EnginesPanel.tsx:82); this panel was
  // missed until 2026-09-13.
  const daySymbol = d?.index_symbol ?? "NIFTY";

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        // Wrapped so this raw fetch is counted like every algoApi call.
        const res = await trackRequest(() =>
          fetch(`/api/expiries?symbol=${encodeURIComponent(daySymbol)}`, {
            credentials: "same-origin",
          }),
        );
        if (res.ok && !cancelled) {
          setExpiries(((await res.json()) as { expiries: string[] }).expiries ?? []);
        }
      } catch {
        /* strip simply stays hidden */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [daySymbol]);

  const todayIso = new Date().toISOString().slice(0, 10);
  const currentWeekly = expiries.filter((e) => e >= todayIso).sort()[0] ?? null;

  return (
    <div className="flex flex-col gap-4">
      {/* ── Master kill ── */}
      <div className="panel px-4 py-3 border border-ce/40 bg-red-950/20 flex items-center gap-3">
        <Switch
          on={g.master_kill}
          danger
          onChange={(v) => mutate((doc) => void (doc.global.master_kill = v))}
          title="Master kill switch — engine-wide"
        />
        <div>
          <div className="text-[12.5px] font-bold text-ce">
            MASTER KILL SWITCH — engine-wide {g.master_kill ? "· KILLED" : "· trading permitted"}
          </div>
          <div className="text-[11px] text-muted">
            Overrides every day/zone switch below. <b>ON: a running trade is exited at
            market the moment you save</b>, and no new trade is taken for as long as it
            stays ON. <b>OFF: trading resumes</b> from the next minute. Day and zone kills
            below work the same way in their own scope — a day kill exits today&apos;s
            running trade (including one carried overnight), a zone kill exits the trade
            that zone opened.
          </div>
        </div>
      </div>

      {/* ── Overnight carry (Pine parity) ── */}
      <div className="panel px-4 py-3 border border-amber-600/40 bg-amber-950/15 flex items-center gap-3">
        <Switch
          on={g.overnight_carry}
          onChange={(v) => mutate((doc) => void (doc.global.overnight_carry = v))}
          title="Overnight carry — Pine parity"
        />
        <div>
          <div className="text-[12.5px] font-bold text-amber-300">
            OVERNIGHT CARRY (Pine parity)
            {g.overnight_carry ? " · positions ride the gap" : " · OFF — End-Exit closes the day"}
          </div>
          <div className="text-[11px] text-muted">
            ON = exactly TradingView: only the Pine exit ladder (Max SL / trail /
            System B / target / Base SL) closes a position — it carries overnight,
            End-Exit is ignored, broker orders are NRML, and the ONLY automatic
            close is the expiry-day 15:25 IST force-close. OFF = the spec's
            End-Exit day-close behavior.
          </div>
        </div>
      </div>

      {showLiveSnapshot && <LiveDecisionSnapshot />}

      <div className="grid md:grid-cols-2 gap-4">
        {/* ── Kill grid ── */}
        <Card title="Master Kill Grid" hint="20 switches, one glance — click to toggle">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-muted">
                <th className="text-left font-medium pb-1"></th>
                {ZONE_IDS.map((z) => (
                  <th key={z} className="font-medium pb-1">{z}</th>
                ))}
                <th className="font-medium pb-1">Day</th>
              </tr>
            </thead>
            <tbody>
              {WEEKDAYS.map((w) => {
                const dc = draft.days[w];
                return (
                  <tr key={w} className="border-t border-border/40">
                    <td className="py-1.5 font-semibold text-gray-200">{WEEKDAY_LABEL[w]}</td>
                    {ZONE_IDS.map((z) => {
                      const killed = dc.zones[z]?.zone_kill ?? false;
                      return (
                        <td key={z} className="text-center">
                          <button
                            type="button"
                            title={`${WEEKDAY_LABEL[w]} ${z}: ${killed ? "killed" : "live"} — ON exits the trade this zone opened and blocks new ones; OFF resumes`}
                            onClick={() =>
                              mutate((doc) => {
                                const zc = doc.days[w].zones[z];
                                if (zc) zc.zone_kill = !zc.zone_kill;
                              })
                            }
                            className={`inline-block w-3 h-3 rounded-full ${
                              killed ? "bg-ce" : "bg-pe"
                            }`}
                          />
                        </td>
                      );
                    })}
                    <td className="text-center">
                      <button
                        type="button"
                        title={`${WEEKDAY_LABEL[w]}: ${dc.day_kill ? "day killed" : "live"} — ON exits the running trade and blocks new ones; OFF resumes`}
                        onClick={() => mutate((doc) => void (doc.days[w].day_kill = !doc.days[w].day_kill))}
                        className={`inline-block w-3 h-3 rounded-full ${
                          dc.day_kill ? "bg-ce" : "bg-pe"
                        }`}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>

        {/* ── Account & sizing ── */}
        <Card title="Account & Position Sizing" hint="global — feeds every day's allocation">
          <div className="flex items-end gap-3 mb-2">
            <NumField
              label="Demat Account Balance (₹)"
              value={g.demat_balance}
              onChange={(v) => mutate((doc) => void (doc.global.demat_balance = v))}
              width="w-44"
            />
            <div className="text-[10.5px] text-muted pb-2">
              Placeholder until the broker API supplies it live.
            </div>
          </div>
          <CalcNote>
            <b>No minimum capital.</b> Each day picks All-In (100% of remaining balance) or an
            allocation %. Lots at entry = allocated capital ÷ (live premium × lot size) — never a
            fixed input. Max concurrent positions: <b>1 (fixed)</b>.
          </CalcNote>
          <div className="mt-2">
            <ToggleLine
              name="Paper Trading Mode"
              desc="Route every order to the simulator instead of the broker"
              on={g.paper.paper_mode}
              onChange={(v) => mutate((doc) => void (doc.global.paper.paper_mode = v))}
            />
          </div>
          {g.paper.paper_mode && (
            <div className="mt-1 text-center font-extrabold text-[11.5px] tracking-wide text-black bg-amber-400 rounded-md py-1.5">
              ⚠ PAPER MODE ACTIVE — orders are simulated, not live
            </div>
          )}
        </Card>
      </div>

      <CalcNote tone="warn">
        <b>Entry vs exit:</b> OI Change, Multi-TF and Ratio decide <b>entry only</b> via the
        automatic unanimous-among-enabled rule. NIFTY Ultra Master Pro runs zero calculation for a
        zone until that rule confirms CALL or PUT; once a trade is open, filter flips are ignored
        and exit is exclusively Ultra Master Pro's Max SL / Trailing / Target — or the day's
        End-Exit on its last zone.
      </CalcNote>

      {/* ── Expiries at a glance (all expiries + their weekdays) ── */}
      {expiries.length > 0 && (
        <div className="panel px-4 py-2.5 flex items-center gap-2 flex-wrap text-[11px]">
          <b className="text-accent mr-1">{daySymbol} expiries:</b>
          {expiries.slice(0, 10).map((e) => (
            <span
              key={e}
              className={`px-2 py-0.5 rounded-full border font-mono ${
                e === currentWeekly
                  ? "text-pe border-pe/50 bg-pe/10 font-bold"
                  : e < todayIso
                    ? "text-muted border-border/60"
                    : "text-gray-200 border-border"
              }`}
              title={e === currentWeekly ? "current weekly — the engine trades THIS contract" : undefined}
            >
              {expiryWithDay(e)}
              {e === currentWeekly ? " · TRADED" : ""}
            </span>
          ))}
          <span className="text-muted ml-1">
            The engine always trades {daySymbol}'s current weekly on this day; dashboards can
            inspect any expiry via the Expiry selector on the engine pages.
          </span>
        </div>
      )}

      {/* ── Day tabs ── */}
      <div className="flex gap-2 flex-wrap">
        {WEEKDAYS.map((w) => (
          <button
            key={w}
            type="button"
            onClick={() => setDay(w)}
            className={`pill ${day === w ? "pill-active" : ""}`}
          >
            {WEEKDAY_LABEL[w]}
            {todayWeekday === w && <span className="ml-1 text-[9px] text-accent">●</span>}
            {draft.days[w].day_kill && <span className="ml-1 text-[9px] text-ce">off</span>}
          </button>
        ))}
      </div>

      {/* ── Copy / Reset (whole day, incl. every indicator's parameters) ── */}
      <div className="panel px-4 py-2.5 flex items-center gap-2 flex-wrap text-[11px]">
        <b className="text-accent">Copy {WEEKDAY_LABEL[day]}'s settings to:</b>
        {WEEKDAYS.filter((w) => w !== day).map((w) => (
          <button
            key={w}
            type="button"
            className="pill text-xs"
            title={`Make ${WEEKDAY_LABEL[w]} identical to ${WEEKDAY_LABEL[day]} — zone TIMES, premium bands, strikes, max trades, kills, risk, index and every engine parameter. Use "Copy settings…" to keep ${WEEKDAY_LABEL[w]}'s own times instead.`}
            onClick={() =>
              mutate((doc) =>
                applyCopy(doc, { kind: "day", day }, [{ day: w, zone: "Z1" }])
              )
            }
          >
            {WEEKDAY_LABEL[w]}
          </button>
        ))}
        <button
          type="button"
          className="pill text-xs border border-accent/40"
          title="Pick any day/zone/indicator scope and any set of targets, with a field-by-field preview"
          onClick={() => setCopyScope({ kind: "day", day })}
        >
          Copy settings…
        </button>
        <span className="text-muted">·</span>
        <button
          type="button"
          className="pill text-xs text-ce border border-ce/30 disabled:opacity-50"
          disabled={!defaults}
          title="Replace this day's config with the Appendix-A reference defaults"
          onClick={() => {
            const src = defaults?.days[day];
            if (src) mutate((doc) => void (doc.days[day] = deepClone(src)));
          }}
        >
          Reset {WEEKDAY_LABEL[day]} to Default
        </button>
        <span className="text-muted">
          a full copy (zone times included) — it stages in the draft, nothing is live until you
          Save (diff shown first)
        </span>
      </div>

      <div className="grid lg:grid-cols-[1.4fr_1fr] gap-4">
        <div className="flex flex-col gap-4">
          {/* ── Zone config ── */}
          <Card
            title={`Zone Config — ${WEEKDAY_LABEL[day]}`}
            hint="times · premium band · max trades · kills"
          >
            <div className="flex items-center gap-3 pb-3 border-b border-border/40 mb-2">
              <span className="text-xs text-muted flex-none">Index for {WEEKDAY_LABEL[day]}</span>
              <div className="w-40">
                <SelectField
                  value={d.index_symbol}
                  options={INDEX_OPTIONS}
                  onChange={(v) => mutate((doc) => void (doc.days[day].index_symbol = v))}
                />
              </div>
              <span
                className="text-xs text-muted flex-none"
                title="How many strikes each side of ATM the live feed COLLECTS for this day (subscription window). Applies within ~1 minute of saving. The TrueData trial cap is ~50 instruments, so beyond ±12 the outer wings are symmetrically truncated (a warning shows on save)."
              >
                Data strikes ATM ±
              </span>
              <div className="w-20">
                <NumField
                  label=""
                  value={d.data_strike_window ?? 11}
                  onChange={(v) =>
                    mutate((doc) =>
                      void (doc.days[day].data_strike_window = Math.max(1, Math.min(60, Math.round(v))))
                    )
                  }
                />
              </div>
              <div className="flex-1" />
              <span className="text-[11px] text-muted">Day kill</span>
              <Switch
                on={d.day_kill}
                danger
                onChange={(v) => mutate((doc) => void (doc.days[day].day_kill = v))}
                title="Day kill — ON exits the running trade (including one carried overnight) and blocks new entries all day; OFF lets it trade again"
              />
            </div>
            {ZONE_IDS.map((zid) => {
              const z = d.zones[zid];
              if (!z) return null;
              const isLast = zid === lastZ;
              return (
                <div
                  key={zid}
                  className={`grid grid-cols-[34px_1fr_1fr_1fr_1fr_58px_58px_72px_60px] gap-2 items-end py-2 border-b border-border/30 last:border-b-0 ${
                    z.zone_kill ? "opacity-50" : ""
                  }`}
                >
                  <div className="text-accent font-bold text-sm pb-1.5">{zid}</div>
                  <TimeField
                    label="Start"
                    value={z.start}
                    onChange={(v) => mutateZone(mutate, day, zid, (zc) => void (zc.start = v))}
                  />
                  <TimeField
                    label="End"
                    value={z.end}
                    onChange={(v) => mutateZone(mutate, day, zid, (zc) => void (zc.end = v))}
                  />
                  <NumField
                    label="Prem Min"
                    value={z.premium_min}
                    onChange={(v) => mutateZone(mutate, day, zid, (zc) => void (zc.premium_min = v))}
                  />
                  <NumField
                    label="Prem Max"
                    value={z.premium_max}
                    onChange={(v) => mutateZone(mutate, day, zid, (zc) => void (zc.premium_max = v))}
                  />
                  <div
                    title={
                      "Multi-strike hunting: the N in-band strikes nearest the band midpoint each hunt in parallel — the first valid entry wins. 1 = single-strike (original behaviour). " +
                      `Up to ${STRIKE_SCAN_MAX}; this day collects ${2 * (d.data_strike_window ?? 11) + 1} strikes per side (Data strikes ATM ±${d.data_strike_window ?? 11}), so that is the most that can actually hunt.`
                    }
                  >
                    <NumField
                      label="Strikes"
                      value={z.strike_scan_count ?? 1}
                      onChange={(v) =>
                        mutateZone(mutate, day, zid, (zc) =>
                          void (zc.strike_scan_count = Math.max(1, Math.min(STRIKE_SCAN_MAX, Math.round(v))))
                        )
                      }
                    />
                    {(z.strike_scan_count ?? 1) > 2 * (d.data_strike_window ?? 11) + 1 && (
                      <div className="text-[9.5px] text-amber-300 leading-tight mt-0.5">
                        only {2 * (d.data_strike_window ?? 11) + 1} collected
                      </div>
                    )}
                  </div>
                  <NumField
                    label="Max Trades"
                    value={z.max_trades}
                    onChange={(v) =>
                      mutateZone(mutate, day, zid, (zc) => void (zc.max_trades = Math.round(v)))
                    }
                  />
                  <div className="flex flex-col items-center gap-1 pb-0.5">
                    <span
                      className={`text-[8.5px] ${
                        isLast && g.overnight_carry ? "text-amber-300/80" : "text-muted"
                      }`}
                    >
                      {isLast ? (g.overnight_carry ? "End-Exit ignored" : "End-Exit") : "End-Exit N/A"}
                    </span>
                    {isLast ? (
                      <Switch
                        on={d.end_exit_enabled}
                        disabled={g.overnight_carry}
                        onChange={(v) =>
                          mutate((doc) => void (doc.days[day].end_exit_enabled = v))
                        }
                        title={
                          g.overnight_carry
                            ? "IGNORED while OVERNIGHT CARRY is ON — the Pine exit ladder alone closes a position and it rides overnight. Switch Overnight Carry off (top of this page) to use End-Exit."
                            : "Force-exit an open trade when this (last) zone's End time candle closes"
                        }
                      />
                    ) : (
                      <span
                        className="text-[10px] text-muted/50"
                        title="End-Exit only ever applies to the day's last zone (§2.3)"
                      >
                        —
                      </span>
                    )}
                  </div>
                  <div className="flex flex-col items-center gap-1 pb-0.5">
                    <span className="text-[8.5px] text-muted">{z.zone_kill ? "Killed" : "Live"}</span>
                    <Switch
                      on={!z.zone_kill}
                      onChange={(v) =>
                        mutateZone(mutate, day, zid, (zc) => void (zc.zone_kill = !v))
                      }
                      title="Zone kill — ON exits the running trade this zone opened and blocks new entries in it; OFF lets it trade again"
                    />
                  </div>
                </div>
              );
            })}
          </Card>

          {/* ── Risk & alerts ── */}
          <RiskCard day={day} d={d} demat={g.demat_balance} mutate={mutate} />
        </div>

        {/* ── Indicator settings per zone ── */}
        <div className="flex flex-col gap-4">
          <Card
            title={`Indicator Settings — ${WEEKDAY_LABEL[day]}`}
            hint="per zone · engines configure in their own sub-tabs"
          >
            {ZONE_IDS.map((zid) => {
              const z = d.zones[zid];
              if (!z) return null;
              const enabledCount = z.enabled_indicators.length;
              return (
                <div
                  key={zid}
                  className={`border border-border/60 rounded-xl px-3 py-2 mb-3 last:mb-0 ${
                    z.zone_kill ? "opacity-50" : ""
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-accent font-bold text-xs">{zid}</span>
                    <span className="text-[10.5px] text-muted">
                      {z.start}–{z.end}
                    </span>
                    <div className="flex-1" />
                    <span className="text-[10px] text-muted">Reeval</span>
                    <div className="w-32">
                      <SelectField
                        value={z.reeval_cadence}
                        options={[
                          { value: "every_candle", label: "Every candle" },
                          { value: "zone_start", label: "Once at start" },
                        ]}
                        onChange={(v) =>
                          mutateZone(mutate, day, zid, (zc) => void (zc.reeval_cadence = v))
                        }
                      />
                    </div>
                    <span
                      className="text-[10px] text-muted"
                      title="EXPERIMENTAL: keep a hunt alive this many minutes through NO_TRADE flickers (an opposite signal still discards instantly). 0 = strict §5.3."
                    >
                      Hold
                    </span>
                    <div className="w-16">
                      <input
                        type="number"
                        min={0}
                        max={375}
                        className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm w-full focus:outline-none focus:border-accent"
                        value={z.direction_hold_min ?? 0}
                        onChange={(e) =>
                          mutateZone(mutate, day, zid, (zc) =>
                            void (zc.direction_hold_min = Math.max(0, Math.min(375, Math.round(Number(e.target.value) || 0))))
                          )
                        }
                      />
                    </div>
                    <span
                      className="text-[10px] text-muted"
                      title="Fresh OI-integrated entry calculation. ON: every OI signal / direction change starts a FRESH Ultra Master Pro entry calculation from that minute (levels keep running; nothing before the signal can shape the entry) — chart, replay, backtest, paper and live alike. OFF: this zone calculates no automated entries (an open trade is still managed to its exit); independent UMP entries stay visible on the chart."
                    >
                      Fresh OI
                    </span>
                    <Switch
                      on={z.oi_fresh_entries ?? true}
                      onChange={(v) => mutateZone(mutate, day, zid, (zc) => void (zc.oi_fresh_entries = v))}
                      title={`Fresh OI-integrated entry calculation — ${(z.oi_fresh_entries ?? true) ? "ON" : "OFF: no automated entries in this zone"}`}
                    />
                  </div>
                  {(Object.keys(INDICATOR_LABEL) as (keyof typeof INDICATOR_LABEL)[]).map((ind) => {
                    const on = z.enabled_indicators.includes(ind);
                    const basket =
                      ind === "oi_change"
                        ? z.oi_structure.strikes_atm_window
                        : ind === "multi_tf"
                          ? z.mtf_ratio.strikes_atm_window
                          : z.mqae.strikes_atm_window;
                    const basketOptions = [
                      { value: "5", label: "ATM ±5" },
                      { value: "10", label: "ATM ±10" },
                      { value: "15", label: "ATM ±15" },
                      { value: "20", label: "ATM ±20" },
                      { value: "-1", label: "All strikes" },
                    ];
                    if (!basketOptions.some((o) => o.value === String(basket))) {
                      basketOptions.unshift({ value: String(basket), label: `ATM ±${basket}` });
                    }
                    return (
                      <div
                        key={ind}
                        className="flex items-center justify-between py-1 border-b border-border/30 last:border-b-0"
                      >
                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            className="text-[11.5px] font-semibold text-gray-200 hover:text-accent hover:underline text-left"
                            title="Open this engine's dashboard for this zone"
                            onClick={() => onOpenEngine?.(ind, day, zid)}
                          >
                            {INDICATOR_LABEL[ind]} ↗
                          </button>
                          <button
                            type="button"
                            className="text-[11px] text-muted hover:text-accent px-1"
                            title={`Copy ${INDICATOR_LABEL[ind]}'s parameters from ${WEEKDAY_LABEL[day]} ${zid} to other zones/days…`}
                            onClick={() =>
                              setCopyScope({ kind: "indicator", day, zone: zid, engine: IND_ENGINE[ind] })
                            }
                          >
                            ⧉
                          </button>
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className="text-[9.5px] text-muted"
                            title="How many strikes this indicator sums into its OI series (ATM ± N of the COLLECTED data; 'All strikes' = the full stored chain). The collected range itself is 'Data strikes ATM ±' above."
                          >
                            strikes
                          </span>
                          <div className="w-28">
                            <SelectField
                              value={String(basket)}
                              options={basketOptions}
                              onChange={(v) =>
                                mutateZone(mutate, day, zid, (zc) => {
                                  const n = Number(v);
                                  if (ind === "oi_change") zc.oi_structure.strikes_atm_window = n;
                                  else if (ind === "multi_tf") zc.mtf_ratio.strikes_atm_window = n;
                                  else zc.mqae.strikes_atm_window = n;
                                })
                              }
                            />
                          </div>
                          <Switch
                            on={on}
                            onChange={(v) =>
                              mutateZone(mutate, day, zid, (zc) => {
                                zc.enabled_indicators = v
                                  ? [...zc.enabled_indicators, ind]
                                  : zc.enabled_indicators.filter((x) => x !== ind);
                              })
                            }
                          />
                        </div>
                      </div>
                    );
                  })}
                  {/* How the switched-on indicators above become one decision.
                      A switched-off indicator never votes at all. */}
                  <div className="flex items-center gap-2 py-1 border-t border-border/50 mt-1 pt-2 flex-wrap">
                    <span
                      className="text-[10px] text-muted"
                      title="Unanimous: every switched-on indicator with a direction must agree. Majority: the side with more votes wins, and a tie is No Trade."
                    >
                      Combine
                    </span>
                    <div className="w-28">
                      <SelectField
                        value={z.combine_rule ?? "unanimous"}
                        options={[
                          { value: "unanimous", label: "Unanimous" },
                          { value: "majority", label: "Majority" },
                        ]}
                        onChange={(v) =>
                          mutateZone(mutate, day, zid, (zc) => void (zc.combine_rule = v))
                        }
                      />
                    </div>
                    <span
                      className="text-[10px] text-muted"
                      title="What a switched-on indicator that currently has no direction means. Blocker: it stops the trade. Abstention: it is ignored and the others decide."
                    >
                      Neutral =
                    </span>
                    <div className="w-32">
                      <SelectField
                        value={z.neutral_mode ?? "block"}
                        options={[
                          { value: "block", label: "Blocker" },
                          { value: "abstain", label: "Abstention" },
                        ]}
                        onChange={(v) =>
                          mutateZone(mutate, day, zid, (zc) => void (zc.neutral_mode = v))
                        }
                      />
                    </div>
                    {(z.combine_rule ?? "unanimous") === "majority" &&
                      z.enabled_indicators.length > 0 &&
                      z.enabled_indicators.length % 2 === 0 && (
                        <span className="text-[10px] text-amber-400">
                          {z.enabled_indicators.length} enabled — a split vote ties, and a tie is No
                          Trade
                        </span>
                      )}
                  </div>
                  <div className="flex items-center justify-between py-1">
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        className="text-[11.5px] font-semibold text-gray-200 hover:text-accent hover:underline text-left"
                        title="Open the Ultra Master Pro dashboard for this zone"
                        onClick={() => onOpenEngine?.("ump", day, zid)}
                      >
                        NIFTY ULTRA MASTER PRO ↗
                      </button>
                      <button
                        type="button"
                        className="text-[11px] text-muted hover:text-accent px-1"
                        title={`Copy Ultra Master Pro's parameters from ${WEEKDAY_LABEL[day]} ${zid} to other zones/days…`}
                        onClick={() =>
                          setCopyScope({ kind: "indicator", day, zone: zid, engine: "ump" })
                        }
                      >
                        ⧉
                      </button>
                    </div>
                    <div className="flex items-center gap-3">
                      <span
                        className="text-[10px] text-muted"
                        title="OFF = observation mode (§2.4): the filter indicators still evaluate and record signals, but the execution engine is locked — no hunts, no entries in this zone."
                      >
                        Strategy Active
                      </span>
                      <Switch
                        on={z.strategy_active}
                        onChange={(v) =>
                          mutateZone(mutate, day, zid, (zc) => void (zc.strategy_active = v))
                        }
                      />
                    </div>
                  </div>
                  <CalcNote>
                    <b>Rule:</b>{" "}
                    {(() => {
                      // Must describe the CONFIGURED rule. A fixed sentence here
                      // would state unanimity while the zone runs majority.
                      const majority = (z.combine_rule ?? "unanimous") === "majority";
                      const blocks = (z.neutral_mode ?? "block") === "block";
                      const neutral = blocks
                        ? " A switched-on indicator with no direction blocks the trade."
                        : " A switched-on indicator with no direction is ignored; the rest decide.";
                      if (enabledCount === 0)
                        return "0 indicators enabled — this zone can NEVER produce a direction.";
                      if (enabledCount === 1)
                        return `1 indicator enabled — its reading is the entry decision directly.${neutral}`;
                      return majority
                        ? `${enabledCount} of 3 enabled — the side with more votes wins; a tie is No Trade.${neutral}`
                        : `${enabledCount} of 3 enabled — all must agree on CALL or PUT, otherwise No Trade.${neutral}`;
                    })()}
                  </CalcNote>
                  <div className="flex items-center gap-1.5 flex-wrap pt-1.5 text-[10px]">
                    <span className="text-muted">
                      copy {zid} (band, trades, all engine params — times stay) to:
                    </span>
                    {ZONE_IDS.filter((z) => z !== zid).map((z) => (
                      <button
                        key={z}
                        type="button"
                        className="pill text-[10px] px-2 py-0.5"
                        onClick={() =>
                          mutate((doc) => copyZoneInto(doc, day, zid, day, z))
                        }
                      >
                        {z}
                      </button>
                    ))}
                    <button
                      type="button"
                      className="pill text-[10px] px-2 py-0.5"
                      title={`Copy ${zid} onto every other day's ${zid}`}
                      onClick={() =>
                        mutate((doc) => {
                          for (const w of WEEKDAYS) {
                            if (w !== day) copyZoneInto(doc, day, zid, w, zid);
                          }
                        })
                      }
                    >
                      {zid} on all days
                    </button>
                    <button
                      type="button"
                      className="pill text-[10px] px-2 py-0.5 border border-accent/40"
                      title={`Copy ${zid} to any day × zone, with a preview`}
                      onClick={() => setCopyScope({ kind: "zone", day, zone: zid })}
                    >
                      Copy {zid} to…
                    </button>
                  </div>
                </div>
              );
            })}
          </Card>
        </div>
      </div>

      {copyScope && (
        <CopySettingsDialog
          draft={draft}
          mutate={mutate}
          initialScope={copyScope}
          onClose={() => setCopyScope(null)}
        />
      )}
    </div>
  );
}

function LiveDecisionSnapshot() {
  const [status, setStatus] = useState<OrchestratorStatus | null>(null);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeMsg, setResumeMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await algoApi.orchestratorStatus());
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(t);
  }, [load]);

  if (!status) return null;
  if (!status.running) {
    return (
      <Card title="Live Decision Snapshot">
        <div className="text-xs text-muted">
          The orchestrator is not running in this backend process (replay mode, or the engine
          loop has not started). Signals, gates and positions appear here once it is live.
        </div>
      </Card>
    );
  }

  const pos = status.position as Record<string, unknown> | null | undefined;
  return (
    <Card title="Live Decision Snapshot" hint={`last evaluated ${status.last_evaluated?.slice(11, 16) ?? "—"}`}>
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
        <Snap
          label="Engine State"
          value={
            (status.state ?? "—").replace("_", " ") +
            (status.state === "hunting" && (status.hunting_strikes ?? 0) > 1
              ? ` (${status.hunting_strikes} strikes)`
              : "")
          }
          accent
        />
        <Snap label="Active Zone" value={status.active_zone || "—"} />
        <Snap
          label="Direction"
          value={status.direction ?? "—"}
          cls={
            status.direction === "CALL"
              ? "text-pe"
              : status.direction === "PUT"
                ? "text-ce"
                : "text-gray-300"
          }
        />
        <Snap label="Trades Today" value={String(status.trades_today ?? 0)} />
        <Snap
          label="Day P&L"
          value={`${(status.realized_pnl_today ?? 0) >= 0 ? "+" : ""}₹${Math.round(status.realized_pnl_today ?? 0).toLocaleString("en-IN")}`}
          cls={(status.realized_pnl_today ?? 0) >= 0 ? "text-pe" : "text-ce"}
        />
        <Snap
          label="Position"
          value={pos ? `${pos["side"]} ${pos["lots"]}L @ ${Number(pos["entry"]).toFixed(2)}` : "flat"}
        />
      </div>
      {status.readings && Object.keys(status.readings).length > 0 && (
        <div className="flex gap-2 flex-wrap mt-3 text-[10.5px]">
          {Object.entries(status.readings).map(([k, v]) => (
            <span
              key={k}
              className={`px-2 py-0.5 rounded-full border border-border font-bold ${
                v === "CALL" ? "text-pe" : v === "PUT" ? "text-ce" : "text-muted"
              }`}
            >
              {INDICATOR_LABEL[k as keyof typeof INDICATOR_LABEL] ?? k}: {v}
            </span>
          ))}
        </div>
      )}
      {(status.gate_blocks?.length ?? 0) > 0 && (
        <div className="mt-2 text-[11px] text-amber-300/90">
          ⚠ {status.gate_blocks!.join(" · ")}
        </div>
      )}
      {(status.paused_reason ||
        (status.gate_blocks ?? []).some((b) => b.includes("max daily loss"))) && (
        <div className="mt-2 flex items-center gap-3 flex-wrap">
          {status.paused_reason && (
            <span className="text-[11px] font-bold text-ce">⏸ {status.paused_reason}</span>
          )}
          <button
            type="button"
            className="pill text-xs"
            disabled={resumeBusy}
            title="Lifts the loss-streak pause AND today's max-loss kill. Both limits then count again from this moment."
            onClick={async () => {
              setResumeBusy(true);
              setResumeMsg(null);
              try {
                const r = await algoApi.resumeEngine();
                setResumeMsg({
                  ok: true,
                  text: `Resumed at ${r.at} — trading restarts from the next minute; loss limits now count from here (day P&L ₹${r.realized.toFixed(0)}).`,
                });
                await load();
              } catch (e) {
                setResumeMsg({ ok: false, text: `Resume failed: ${e instanceof Error ? e.message : String(e)}` });
              } finally {
                setResumeBusy(false);
              }
            }}
          >
            {resumeBusy ? "Resuming…" : "Resume engine"}
          </button>
        </div>
      )}
      {resumeMsg && (
        <div className={`mt-1 text-[11px] ${resumeMsg.ok ? "text-pe" : "text-ce"}`}>{resumeMsg.text}</div>
      )}
      <div className="mt-3">
        <LiveDecisionTrace zone={status.active_zone ?? ""} />
      </div>
    </Card>
  );
}

function Snap(props: { label: string; value: string; cls?: string; accent?: boolean }) {
  return (
    <div className="bg-panel/60 border border-border rounded-lg px-3 py-2">
      <div className="text-[9.5px] text-muted uppercase tracking-wide">{props.label}</div>
      <div className={`text-sm font-bold ${props.cls ?? (props.accent ? "text-accent" : "text-gray-100")}`}>
        {props.value}
      </div>
    </div>
  );
}

function mutateZone(
  mutate: Props["mutate"],
  day: Weekday,
  zid: ZoneId,
  fn: (z: ZoneConfig) => void
): void {
  mutate((doc) => {
    const z = doc.days[day].zones[zid];
    if (z) fn(z);
  });
}

function RiskCard(props: {
  day: Weekday;
  d: DayConfig;
  demat: number;
  mutate: Props["mutate"];
}) {
  const { day, d, demat, mutate } = props;
  const allocated = demat * (d.all_in ? 100 : d.allocation_pct) / 100;
  return (
    <Card title={`Risk & Alerts — ${WEEKDAY_LABEL[day]}`} hint="this day only">
      <ToggleLine
        name="All In"
        desc="Use 100% of whatever balance remains in the Demat account"
        on={d.all_in}
        onChange={(v) => mutate((doc) => void (doc.days[day].all_in = v))}
      />
      <div className="grid grid-cols-2 gap-3 py-2">
        <NumField
          label="Capital Allocation %"
          value={d.allocation_pct}
          suffix="%"
          disabled={d.all_in}
          onChange={(v) => mutate((doc) => void (doc.days[day].allocation_pct = v))}
        />
        <NumField
          label="Max Loss Per Day (% of allocated)"
          value={d.max_loss_pct}
          suffix="%"
          onChange={(v) => mutate((doc) => void (doc.days[day].max_loss_pct = v))}
        />
        <NumField
          label="Max Profit Lock (% of allocated)"
          value={d.max_profit_lock_pct}
          suffix="%"
          onChange={(v) => mutate((doc) => void (doc.days[day].max_profit_lock_pct = v))}
        />
        <NumField
          label="Max Consecutive Losses (trades)"
          value={d.max_consec_losses}
          onChange={(v) => mutate((doc) => void (doc.days[day].max_consec_losses = Math.round(v)))}
        />
        <NumField
          label="Max Consecutive Loss (% of allocated)"
          value={d.max_consec_loss_pct}
          suffix="%"
          onChange={(v) => mutate((doc) => void (doc.days[day].max_consec_loss_pct = v))}
        />
      </div>
      <CalcNote>
        ≈ {fmtRupee(allocated)} allocated · max loss ≈ {fmtRupee((allocated * d.max_loss_pct) / 100)} ·
        profit lock ≈ {fmtRupee((allocated * d.max_profit_lock_pct) / 100)} · streak cap ≈{" "}
        {fmtRupee((allocated * d.max_consec_loss_pct) / 100)}
      </CalcNote>
      <div className="mt-2 pt-1 border-t border-border/40">
        <ToggleLine
          name="Telegram alert on trade entry/exit"
          desc="Entry/exit fills, kill switches and the 15:45 daily summary — Telegram is the only alert channel"
          on={d.alerts.telegram_trade_entry_exit}
          onChange={(v) =>
            mutate((doc) => void (doc.days[day].alerts.telegram_trade_entry_exit = v))
          }
        />
      </div>
    </Card>
  );
}
