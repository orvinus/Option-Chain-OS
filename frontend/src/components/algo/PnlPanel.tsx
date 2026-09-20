/**
 * Sub-tab — P&L Summary (§12): cumulative stats, the month calendar (net ₹ +
 * % of that day's allocated capital per cell — the SAME units the risk
 * limits use), by-day and by-zone tables, and the detailed trade log with a
 * date filter, ledger filter and CSV/Excel export of the visible rows, plus
 * the "Export…" dialog for complete range exports (§6). Live and paper are
 * never mixed in one aggregate.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { algoApi } from "../../api/algoRest";
import type { PnlCalendar, PnlSummary, TradeRow } from "../../types/algo";
import { exitReasonLabel } from "../../types/algo";
import type { ExportTable } from "../../utils/exportData";
import { ExportButton } from "../ExportButton";
import { CalcNote, Card, DateField, SelectField } from "./controls";
import { ExportDialog } from "./ExportDialog";
import { useAlgoStreamContext } from "../../hooks/useAlgoStream";
import { inr2, pnlTone, positionLabel, streamPosition } from "./streamPosition";

/** Visible trade-log rows → export table (RFC-4180 escaping happens in toCsv). */
function tradesTable(trades: TradeRow[]): ExportTable {
  const headers = [
    "date", "day", "zone", "index", "side", "strike", "expiry", "entry_ts", "entry",
    "exit_ts", "exit", "lots", "pnl", "pnl_pct", "exit_reason", "exit_reason_label",
    "ledger", "scenario", "fees_total",
  ];
  const rows = trades.map((t) => [
    t.trade_date, t.day, t.zone_id, t.index_symbol, t.side, t.strike, t.expiry,
    t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.lots, t.pnl_rupees, t.pnl_pct,
    t.exit_reason, exitReasonLabel(t.exit_reason), t.ledger, t.sub_scenario,
    t.fees
      ? "total" in t.fees
        ? t.fees.total
        : Object.values(t.fees).reduce((a, b) => a + b, 0)
      : null,
  ]);
  return { headers, rows };
}

/** One-line meaning for each Cumulative Performance tile (meeting 17–20 min). */
const STAT_HINT = {
  pnl: "Net ₹ of every closed trade in the period (after fees). Open positions are excluded.",
  blended: "Period net P&L ÷ the sum of each traded day's allocated capital — never an average of daily percentages.",
  winRate: "Closed trades with net P&L > 0 as a share of all closed trades (W / L counts beside it).",
  trades: "Number of closed trades in the period on this ledger.",
  avgWin: "Mean net ₹ of the winning trades.",
  avgLoss: "Mean net ₹ of the losing trades.",
  profitFactor: "Gross ₹ won ÷ gross ₹ lost. Above 1 = profitable; '—' when nothing was lost.",
  drawdown:
    "Largest peak-to-trough fall of the equity curve (ledger balance + cumulative closed P&L), in ₹ and as % of the peak. Dates = the peak → trough exits.",
} as const;

const DAY_LABEL: Record<string, string> = {
  monday: "Monday", tuesday: "Tuesday", wednesday: "Wednesday",
  thursday: "Thursday", friday: "Friday",
};

function rupee(n: number | null | undefined): string {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}₹${Math.round(n).toLocaleString("en-IN")}`;
}

function pnlCls(n: number | null | undefined): string {
  if (n == null || n === 0) return "text-muted";
  return n > 0 ? "text-pe" : "text-ce";
}

export function PnlPanel({ backtestRunId }: { backtestRunId?: number } = {}) {
  const isBacktest = backtestRunId != null;
  const [ledger, setLedger] = useState<"paper" | "live">("paper");
  const [summary, setSummary] = useState<PnlSummary | null>(null);
  const [calendar, setCalendar] = useState<PnlCalendar | null>(null);
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [dateFilter, setDateFilter] = useState("");
  const [zoneFilter, setZoneFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Per-day journal notes for the visible month ({date: note}).
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [noteDate, setNoteDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [noteText, setNoteText] = useState("");
  const [noteBusy, setNoteBusy] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  // The open position (if any), from the page's live stream. Closed-trade
  // tiles below cannot include it; this card makes it visible instead of
  // leaving "Total Trades 0" as the only thing on screen during a trade.
  const stream = useAlgoStreamContext();
  const openPos = isBacktest ? null : streamPosition(stream?.frame);
  const openHere = openPos != null && openPos.ledger === ledger;

  const load = useCallback(async () => {
    try {
      // Backtest mode: identical shapes from the run-scoped endpoints — every
      // card below renders unchanged. Journal notes stay live-only.
      const [s, c, t, n] = await Promise.all([
        backtestRunId != null
          ? algoApi.backtestPnlSummary(backtestRunId)
          : algoApi.pnlSummary({ ledger }),
        backtestRunId != null
          ? algoApi.backtestPnlCalendar(backtestRunId, month)
          : algoApi.pnlCalendar(month, ledger),
        backtestRunId != null
          ? algoApi.backtestTrades(backtestRunId, {
              date: dateFilter || undefined,
              zone: zoneFilter || undefined,
            })
          : algoApi.trades({
              ledger,
              date: dateFilter || undefined,
              zone: zoneFilter || undefined,
              limit: 500,
            }),
        backtestRunId != null
          ? Promise.resolve({} as Record<string, string>)
          : algoApi.dayNotes(month),
      ]);
      setSummary(s);
      setCalendar(c);
      setTrades(t);
      setNotes(n);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [ledger, month, dateFilter, zoneFilter, backtestRunId]);

  const saveNote = useCallback(async () => {
    if (!noteDate) return;
    setNoteBusy(true);
    try {
      await algoApi.putDayNote(noteDate, noteText);
      setNotes((prev) => {
        const next = { ...prev };
        if (noteText.trim()) next[noteDate] = noteText;
        else delete next[noteDate];
        return next;
      });
    } finally {
      setNoteBusy(false);
    }
  }, [noteDate, noteText]);

  useEffect(() => {
    void load();
  }, [load]);

  const visibleTable = useCallback(
    () => (trades.length ? tradesTable(trades) : null),
    [trades]
  );
  const exportBase = isBacktest
    ? `backtest-${backtestRunId}-trades${dateFilter ? `-${dateFilter}` : ""}`
    : `algo-${ledger}-trades-${dateFilter || new Date().toISOString().slice(0, 10)}`;

  const calendarWeeks = useMemo(() => {
    if (!calendar) return [];
    const [y, m] = calendar.month.split("-").map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1));
    const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
    // Monday-first column index for the 1st.
    const lead = (first.getUTCDay() + 6) % 7;
    const cells: ({ date: string; dom: number } | null)[] = Array(lead).fill(null);
    for (let d = 1; d <= daysInMonth; d++) {
      cells.push({ date: `${calendar.month}-${String(d).padStart(2, "0")}`, dom: d });
    }
    while (cells.length % 7 !== 0) cells.push(null);
    const weeks = [];
    for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
    return weeks;
  }, [calendar]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {isBacktest && (
          <span className="pill pill-active">BACKTEST run #{backtestRunId}</span>
        )}
        {!isBacktest && (["paper", "live"] as const).map((l) => (
          <button
            key={l}
            type="button"
            onClick={() => setLedger(l)}
            className={`pill ${ledger === l ? "pill-active" : ""}`}
          >
            {l === "paper" ? "PAPER ledger" : "LIVE ledger"}
          </button>
        ))}
        {error && <span className="text-xs text-ce ml-2">{error}</span>}
      </div>

      {openPos && !openHere && (
        <div className="text-[11px] text-amber-300 bg-amber-950/30 border border-amber-700/40 rounded-lg px-3 py-1.5">
          An open {openPos.ledger.toUpperCase()} position exists (trade #{openPos.trade_id},{" "}
          {positionLabel(openPos)}) — switch to the {openPos.ledger.toUpperCase()} ledger to see it.
        </div>
      )}
      {openHere && openPos && (
        <Card
          title="Open position"
          hint="not yet in the totals below — they count closed trades only"
        >
          <div data-testid="pnl-open-position" className="grid grid-cols-2 md:grid-cols-6 gap-3">
            <Stat
              label="Contract"
              value={`${openPos.side} ${positionLabel(openPos)}`}
              cls={openPos.side === "CALL" ? "text-pe" : "text-ce"}
              title={`trade #${openPos.trade_id} · ${openPos.zone} · ${openPos.sub_scenario ?? ""}`}
            />
            <Stat label="Lots × lot size" value={`${openPos.lots} × ${openPos.lot_size ?? "—"}`} />
            <Stat
              label="Entry"
              value={`${inr2(openPos.entry)}${openPos.entry_ts ? ` @ ${openPos.entry_ts.slice(11, 16)}` : ""}`}
            />
            <Stat
              label="Last price"
              value={openPos.current_price != null ? inr2(openPos.current_price) : "—"}
              title={openPos.price_ts ? `newest tick ${openPos.price_ts.slice(11, 19)}` : undefined}
            />
            <Stat
              label="Unrealized (gross)"
              value={inr2(openPos.unrealized_rupees, true)}
              cls={pnlTone(openPos.unrealized_rupees)}
            />
            <Stat
              label="Unrealized %"
              value={openPos.unrealized_pct != null ? `${openPos.unrealized_pct > 0 ? "+" : ""}${openPos.unrealized_pct.toFixed(2)}%` : "—"}
              cls={pnlTone(openPos.unrealized_pct)}
            />
          </div>
        </Card>
      )}

      {/* Cumulative stats */}
      <Card title="Cumulative Performance" hint={`${summary?.from_date ?? ""} → ${summary?.to_date ?? ""}`}>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat label="Period P&L" value={rupee(summary?.pnl)} cls={pnlCls(summary?.pnl)} title={STAT_HINT.pnl} />
          <Stat
            label="Blended Return"
            value={summary?.pnl_pct_blended != null ? `${summary.pnl_pct_blended.toFixed(2)}%` : "—"}
            cls={pnlCls(summary?.pnl_pct_blended)}
            title={STAT_HINT.blended}
          />
          <Stat
            label="Win Rate"
            value={
              summary?.win_rate != null
                ? `${summary.win_rate}% (${summary.wins}W / ${summary.losses}L)`
                : "—"
            }
            title={STAT_HINT.winRate}
          />
          <Stat
            label="Total Trades"
            value={`${summary?.trades ?? "—"}${openHere ? " closed · 1 open" : ""}`}
            title={STAT_HINT.trades}
          />
          <Stat label="Avg Win" value={rupee(summary?.avg_win)} cls="text-pe" title={STAT_HINT.avgWin} />
          <Stat label="Avg Loss" value={rupee(summary?.avg_loss)} cls="text-ce" title={STAT_HINT.avgLoss} />
          <Stat
            label="Profit Factor"
            value={summary?.profit_factor != null ? String(summary.profit_factor) : "—"}
            title={STAT_HINT.profitFactor}
          />
          <Stat
            label="Max Drawdown"
            value={
              summary && summary.trades > 0 && summary.max_drawdown != null
                ? `−₹${Math.round(Math.abs(summary.max_drawdown)).toLocaleString("en-IN")}${
                    summary.max_drawdown_pct != null
                      ? ` (${Math.abs(summary.max_drawdown_pct).toFixed(1)}%)`
                      : ""
                  }`
                : "—"
            }
            cls={summary && summary.trades > 0 && summary.max_drawdown ? "text-ce" : "text-muted"}
            title={
              summary?.max_drawdown_from
                ? `${fmtTs(summary.max_drawdown_from)} → ${fmtTs(summary.max_drawdown_to)} · ${STAT_HINT.drawdown}`
                : STAT_HINT.drawdown
            }
          />
        </div>
      </Card>

      {/* Calendar */}
      <Card title="Net Day P&L — Calendar View" hint="₹ + % of that day's allocated capital · click a day to open its log">
        <div className="flex items-center justify-center gap-3 mb-2">
          <button type="button" className="pill text-xs" onClick={() => setMonth(shiftMonth(month, -1))}>
            ‹
          </button>
          <span className="text-sm font-bold">{month}</span>
          <button type="button" className="pill text-xs" onClick={() => setMonth(shiftMonth(month, 1))}>
            ›
          </button>
        </div>
        <div className="grid grid-cols-7 gap-1 text-center text-[9.5px] text-muted uppercase font-bold mb-1">
          {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
            <div key={d}>{d}</div>
          ))}
        </div>
        {calendarWeeks.map((week, wi) => (
          <div key={wi} className="grid grid-cols-7 gap-1 mb-1">
            {week.map((cell, ci) => {
              if (!cell) return <div key={ci} />;
              const info = calendar?.days[cell.date];
              const weekend = ci >= 5;
              const note = notes[cell.date];
              return (
                <button
                  key={ci}
                  type="button"
                  disabled={weekend || (!info && !note)}
                  onClick={() => {
                    if (info) setDateFilter(cell.date);
                    setNoteDate(cell.date);
                    setNoteText(notes[cell.date] ?? "");
                  }}
                  title={note ? `📝 ${note}` : undefined}
                  className={`rounded-lg border px-1.5 py-1.5 text-left min-h-[52px] ${
                    weekend
                      ? "opacity-30 border-border"
                      : info
                        ? info.pnl > 0
                          ? "border-pe/40 bg-pe/5 hover:brightness-125"
                          : info.pnl < 0
                            ? "border-ce/40 bg-ce/5 hover:brightness-125"
                            : "border-border bg-panel hover:brightness-125"
                        : note
                          ? "border-amber-500/40 bg-amber-500/5 hover:brightness-125"
                          : "border-border/40"
                  }`}
                >
                  <div className="text-[10px] text-muted font-bold flex items-center gap-1">
                    {cell.dom}
                    {note && <span className="text-amber-400 text-[9px]" title={note}>📝</span>}
                  </div>
                  {info && (
                    <>
                      <div className={`text-[10.5px] font-bold ${pnlCls(info.pnl)}`}>
                        {rupee(info.pnl)}
                      </div>
                      <div className="text-[9px] text-muted">
                        {info.pnl_pct != null ? `${info.pnl_pct.toFixed(2)}%` : ""}
                      </div>
                    </>
                  )}
                </button>
              );
            })}
          </div>
        ))}
        <CalcNote>
          Each cell's % = that day's net P&L ÷ that day's allocated capital — the same basis as
          the Max Loss/Profit limits. The period total uses the blended figure, never an average
          of daily percentages. Days with a 📝 carry a journal note — click a day (or pick a
          date below) to read or edit it.
        </CalcNote>
      </Card>

      {/* ── Day Notes — the trader's journal line per date (live only) ── */}
      {!isBacktest && (
      <Card title="Day Notes" hint="write a note for any particular day — shows as 📝 on the calendar">
        <div className="flex items-end gap-2 flex-wrap">
          <div className="w-44">
            <DateField
              label="Date"
              value={noteDate}
              onChange={(v) => {
                setNoteDate(v);
                setNoteText(notes[v] ?? "");
              }}
            />
          </div>
          <label className="block flex-1 min-w-[260px]">
            <span className="block text-[10px] text-muted mb-1">
              Note {notes[noteDate] ? "(saved)" : "(new)"}
            </span>
            <textarea
              className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm w-full h-16 focus:outline-none focus:border-accent resize-y"
              placeholder="e.g. expiry-day chop — killed Z2 manually at 12:41; reduced loss cap"
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="pill pill-active text-xs disabled:opacity-50"
            disabled={noteBusy || !noteDate}
            onClick={() => void saveNote()}
          >
            {noteBusy ? "Saving…" : notes[noteDate] && !noteText.trim() ? "Delete note" : "Save note"}
          </button>
        </div>
      </Card>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        <Card title="P&L by Trading Day">
          <GroupTable rows={summary?.by_day ?? []} labeler={(g) => DAY_LABEL[g] ?? g} showAlloc />
        </Card>
        <Card title="P&L by Zone" hint="all days combined">
          <GroupTable rows={summary?.by_zone ?? []} labeler={(g) => g} />
        </Card>
      </div>

      {/* Trade log */}
      <Card title="Detailed Trade Log" hint="every fill — filterable, exportable">
        <div className="flex items-end gap-2 flex-wrap mb-2">
          <div className="w-44">
            <DateField label="Date" value={dateFilter} onChange={setDateFilter} />
          </div>
          <div className="w-28">
            <SelectField
              label="Zone"
              value={zoneFilter}
              options={[
                { value: "", label: "All zones" },
                { value: "Z1", label: "Z1" },
                { value: "Z2", label: "Z2" },
                { value: "Z3", label: "Z3" },
              ]}
              onChange={setZoneFilter}
            />
          </div>
          {dateFilter && (
            <button type="button" className="pill text-xs" onClick={() => setDateFilter("")}>
              Clear date
            </button>
          )}
          <div className="flex-1" />
          <ExportButton
            getTable={visibleTable}
            filenameBase={exportBase}
            disabled={trades.length === 0}
          />
          <button
            type="button"
            className="pill text-xs"
            onClick={() => setExportOpen(true)}
            title="Complete data for any range — trades, decisions, signals, calendar, summary — as CSV or Excel"
          >
            Export…
          </button>
        </div>
        {exportOpen && (
          <ExportDialog
            mode={
              backtestRunId != null
                ? { kind: "backtest", runId: backtestRunId }
                : { kind: "ledger", ledger }
            }
            onClose={() => setExportOpen(false)}
          />
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] min-w-[860px]">
            <thead>
              <tr className="text-muted text-left">
                {["Date", "Day", "Zone", "Index", "Side", "Strike", "Entry", "Exit", "Lots", "P&L", "P&L %", "Exit Reason", "Scenario"].map(
                  (h) => (
                    <th key={h} className="py-1.5 pr-2 font-medium">{h}</th>
                  )
                )}
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.id} className="border-t border-border/30">
                  <td className="py-1.5 pr-2 font-mono">{t.trade_date}</td>
                  <td className="py-1.5 pr-2">{DAY_LABEL[t.day] ?? t.day}</td>
                  <td className="py-1.5 pr-2">{t.zone_id}</td>
                  <td className="py-1.5 pr-2">{t.index_symbol}</td>
                  <td className={`py-1.5 pr-2 font-bold ${t.side === "CALL" ? "text-pe" : "text-ce"}`}>
                    {t.side}
                  </td>
                  <td className="py-1.5 pr-2 font-mono">{t.strike ?? "—"}</td>
                  <td className="py-1.5 pr-2 font-mono">
                    {t.entry_ts.slice(11, 16)} @ {t.entry_price.toFixed(2)}
                  </td>
                  <td className="py-1.5 pr-2 font-mono">
                    {t.exit_ts ? `${t.exit_ts.slice(11, 16)} @ ${t.exit_price?.toFixed(2)}` : "open"}
                  </td>
                  <td className="py-1.5 pr-2 text-right">{t.lots}</td>
                  <td className={`py-1.5 pr-2 text-right font-bold ${pnlCls(t.pnl_rupees)}`}>
                    {!t.exit_ts && openHere && openPos && openPos.trade_id === t.id ? (
                      <span
                        className={`italic ${pnlTone(openPos.unrealized_rupees)}`}
                        title="Unrealized, marked at the newest tick — becomes the realized P&L when it closes."
                      >
                        {inr2(openPos.unrealized_rupees, true)} open
                      </span>
                    ) : (
                      rupee(t.pnl_rupees)
                    )}
                  </td>
                  <td className={`py-1.5 pr-2 text-right ${pnlCls(t.pnl_pct)}`}>
                    {t.pnl_pct != null ? `${t.pnl_pct.toFixed(2)}%` : "—"}
                  </td>
                  <td className="py-1.5 pr-2 text-muted" title={t.exit_reason || undefined}>
                    {exitReasonLabel(t.exit_reason)}
                  </td>
                  <td className="py-1.5 text-muted">{t.sub_scenario}</td>
                </tr>
              ))}
              {trades.length === 0 && (
                <tr>
                  <td colSpan={13} className="py-3 text-muted">
                    {isBacktest
                      ? "No trades in this backtest run (with the current filters)."
                      : `No trades recorded yet on the ${ledger} ledger.`}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function Stat(props: { label: string; value: string; cls?: string; title?: string }) {
  return (
    <div
      className={`bg-panel/60 border border-border rounded-lg px-3 py-2 ${props.title ? "cursor-help" : ""}`}
      title={props.title}
    >
      <div className="text-[9.5px] text-muted uppercase tracking-wide">{props.label}</div>
      <div className={`text-base font-bold ${props.cls ?? "text-gray-100"}`}>{props.value}</div>
    </div>
  );
}

/** "2026-08-20T10:31:00+05:30" → "2026-08-20 10:31"; dates pass through. */
function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.length > 10 ? `${ts.slice(0, 10)} ${ts.slice(11, 16)}` : ts;
}

function GroupTable(props: {
  rows: import("../../types/algo").PnlGroupRow[];
  labeler: (g: string) => string;
  showAlloc?: boolean;
}) {
  const total = props.rows.reduce(
    (acc, r) => ({
      trades: acc.trades + r.trades,
      wins: acc.wins + r.wins,
      pnl: acc.pnl + r.pnl,
    }),
    { trades: 0, wins: 0, pnl: 0 }
  );
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-muted text-left">
          <th className="py-1 font-medium">Group</th>
          <th className="py-1 font-medium text-right">Trades</th>
          <th className="py-1 font-medium text-right">Win Rate</th>
          <th className="py-1 font-medium text-right">P&L</th>
          {props.showAlloc && <th className="py-1 font-medium text-right">P&L %</th>}
        </tr>
      </thead>
      <tbody>
        {props.rows.map((r) => (
          <tr key={r.group} className="border-t border-border/30">
            <td className="py-1.5">{props.labeler(r.group)}</td>
            <td className="py-1.5 text-right">{r.trades}</td>
            <td className="py-1.5 text-right">
              {r.trades ? `${((r.wins / r.trades) * 100).toFixed(1)}%` : "—"}
            </td>
            <td className={`py-1.5 text-right font-bold ${pnlCls(r.pnl)}`}>{rupee(r.pnl)}</td>
            {props.showAlloc && (
              <td className={`py-1.5 text-right ${pnlCls(r.pnl_pct)}`}>
                {r.pnl_pct != null ? `${r.pnl_pct.toFixed(2)}%` : "—"}
              </td>
            )}
          </tr>
        ))}
        {props.rows.length > 0 && (
          <tr className="border-t-2 border-border font-bold">
            <td className="py-1.5">Total</td>
            <td className="py-1.5 text-right">{total.trades}</td>
            <td className="py-1.5 text-right">
              {total.trades ? `${((total.wins / total.trades) * 100).toFixed(1)}%` : "—"}
            </td>
            <td className={`py-1.5 text-right ${pnlCls(total.pnl)}`}>{rupee(total.pnl)}</td>
            {props.showAlloc && <td />}
          </tr>
        )}
        {props.rows.length === 0 && (
          <tr>
            <td colSpan={5} className="py-2 text-muted">No closed trades in the period.</td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}
