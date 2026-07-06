import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useMemo } from "react";
import type { OIChangeResponse, OIChangeRow } from "../types";
import { filterOiRowsByAtmWindow, nearestIdx } from "../utils/oiStrikeWindow";

export type OIMode = "change" | "absolute";

interface Props {
  data: OIChangeResponse | null;
  mode: OIMode;
  atmWindow: number; // number of strikes above and below ATM to show (0 = all)
  isLoading?: boolean;
  underlyingLabel?: string;
  /** From `/api/health` when feed is live — ATM line tracks the index instead of DB `underlying` only. */
  liveSpot?: number | null;
  /** From `/api/health` — avoids blaming "market closed" during the regular session. */
  nseSessionOpen?: boolean;
  /** The symbol's strike step from the registry (NIFTY 50, SENSEX 100). */
  strikeStep?: number | null;
}

// ── formatting helpers ──────────────────────────────────────────────────────

/** Compact Indian number notation — matches Sensibull axis labels */
function compactNum(n: number): string {
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}K`;
  return `${sign}${abs.toLocaleString()}`;
}

// ── main component ──────────────────────────────────────────────────────────

export function OIChangeChart({
  data,
  mode,
  atmWindow,
  isLoading = false,
  underlyingLabel = "NIFTY",
  liveSpot,
  nseSessionOpen,
  strikeStep,
}: Props) {
  const allChangeZero = useMemo(() => {
    if (!data || mode !== "change") return false;
    const spotForWindow = liveSpot ?? data.spot ?? null;
    const windowRows = filterOiRowsByAtmWindow(data.rows, spotForWindow, atmWindow, strikeStep);
    if (windowRows.length === 0) return false;
    return windowRows.every((r) => r.call_oi_change === 0 && r.put_oi_change === 0);
  }, [data, mode, atmWindow, liveSpot, strikeStep]);

  const option: EChartsOption = useMemo(() => {
    const allRows: OIChangeRow[] = data?.rows ?? [];
    const spot = liveSpot ?? data?.spot ?? null;
    const rows = filterOiRowsByAtmWindow(allRows, spot, atmWindow, strikeStep);

    const strikes = rows.map((r) => r.strike);

    // Raw contract values — Sensibull displays OI in contracts (not lots)
    const ceSeries = mode === "change"
      ? rows.map((r) => r.call_oi_change)
      : rows.map((r) => r.call_oi);
    const peSeries = mode === "change"
      ? rows.map((r) => r.put_oi_change)
      : rows.map((r) => r.put_oi);

    const seriesLabel = mode === "change"
      ? { ce: "CE OI Chg", pe: "PE OI Chg" }
      : { ce: "CE OI", pe: "PE OI" };

    // ATM mark-line — use the actual strike VALUE (not array index) so ECharts
    // can locate the correct category on the axis regardless of window filtering.
    const atmIdx = spot != null ? nearestIdx(strikes, spot) : null;
    const atmStrike = atmIdx != null ? strikes[atmIdx] : null;
    const markLine = atmStrike != null && spot != null
      ? {
          symbol: "none",
          silent: true,
          lineStyle: { color: "rgba(255,255,255,0.85)", type: "dashed" as const, width: 1.5 },
          label: {
            show: true,
            position: "insideStartTop" as const,
            formatter: `${underlyingLabel} ${spot.toFixed(2)}`,
            color: "#ffffff",
            backgroundColor: "rgba(15,23,42,0.92)",
            borderColor: "#3b82f6",
            borderWidth: 1,
            padding: [4, 6] as [number, number],
            borderRadius: 6,
            fontSize: 11,
          },
          data: [{ xAxis: atmStrike }],
        }
      : undefined;

    // Sensibull colour convention: CE = red, PE = blue
    // ATM strike gets a highlighted (brighter) shade
    // Negative OI change gets a muted/grey shade
    const CE_COLOR = "#f75c5c";       // red — calls
    const CE_COLOR_ATM = "#ff8c00";   // orange-red — ATM call
    const CE_COLOR_NEG = "#7f3a3a";   // muted red — negative CE change
    const PE_COLOR = "#4da6ff";       // blue — puts
    const PE_COLOR_ATM = "#00d4b4";   // teal — ATM put
    const PE_COLOR_NEG = "#2a4a7f";   // muted blue — negative PE change

    const ceData = ceSeries.map((v, i) => ({
      value: v,
      itemStyle: {
        color: i === atmIdx
          ? CE_COLOR_ATM
          : mode === "change" && v < 0 ? CE_COLOR_NEG : CE_COLOR,
        opacity: 1,
      },
    }));
    const peData = peSeries.map((v, i) => ({
      value: v,
      itemStyle: {
        color: i === atmIdx
          ? PE_COLOR_ATM
          : mode === "change" && v < 0 ? PE_COLOR_NEG : PE_COLOR,
        opacity: 1,
      },
    }));

    // When all changes are 0, still render the chart with 0-height bars but set
    // a minimum Y-axis range so the axes are visible and the chart clearly shows
    // "no data" rather than an invisible chart.
    const yAxisMin = allChangeZero ? -1 : undefined;
    const yAxisMax = allChangeZero ? 1 : undefined;

    return {
      backgroundColor: "transparent",
      animation: true,
      animationDuration: 200,
      grid: { left: 16, right: 16, top: 20, bottom: 60, containLabel: true },
      legend: {
        data: [
          { name: seriesLabel.pe, itemStyle: { color: PE_COLOR } },
          { name: seriesLabel.ce, itemStyle: { color: CE_COLOR } },
        ],
        bottom: 0,
        textStyle: { color: "#9ca3af", fontSize: 12 },
        itemHeight: 10,
        itemWidth: 16,
        itemGap: 24,
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "rgba(10,15,30,0.97)",
        borderColor: "#1e3a5f",
        borderWidth: 1,
        textStyle: { color: "#e5e7eb", fontSize: 12 },
        formatter: (params: unknown) => {
          const arr = params as Array<{ axisValue: string; seriesName: string; data: { value: number }; color: string }>;
          if (!arr?.length) return "";
          const strike = arr[0].axisValue;
          const isAtm = spot != null && Math.abs(Number(strike) - spot) < 30;
          const atmBadge = isAtm ? ` <span style="background:#f97316;color:#000;font-size:9px;padding:1px 5px;border-radius:4px;font-weight:700">ATM</span>` : "";
          const lines = arr.map(
            (p) =>
              `<div style="display:flex;align-items:center;gap:6px;margin-top:3px">` +
              `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${p.color}"></span>` +
              `<span style="color:#9ca3af">${p.seriesName}:</span> <b style="color:#fff">${compactNum(p.data?.value ?? 0)}</b>` +
              `</div>`
          );
          return `<div style="font-size:12px;min-width:160px">` +
            `<div style="margin-bottom:4px">Strike <b style="color:#fff">${strike}</b>${atmBadge}</div>` +
            lines.join("") +
            `</div>`;
        },
      },
      xAxis: {
        type: "category",
        data: strikes,
        axisLine: { lineStyle: { color: "#2d3748" } },
        axisTick: { show: false },
        axisLabel: {
          color: "#6b7280",
          fontSize: 10,
          rotate: strikes.length > 30 ? 45 : 0,
          interval: strikes.length > 40 ? 1 : 0,
        },
        boundaryGap: true,
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        min: yAxisMin,
        max: yAxisMax,
        splitLine: { lineStyle: { color: "rgba(45,55,72,0.6)" } },
        axisLabel: {
          color: "#6b7280",
          fontSize: 11,
          formatter: (v: number) => compactNum(v),
        },
      },
      series: [
        {
          name: seriesLabel.pe,
          type: "bar",
          data: peData,
          barGap: "0%",
          barCategoryGap: "25%",
          markLine,
        },
        {
          name: seriesLabel.ce,
          type: "bar",
          data: ceData,
          barGap: "0%",
          barCategoryGap: "25%",
        },
      ],
    } satisfies EChartsOption;
  }, [data, mode, atmWindow, underlyingLabel, allChangeZero, liveSpot, strikeStep]);

  // ── loading skeleton ────────────────────────────────────────────────────
  if (isLoading && !data) {
    return (
      <div className="panel flex items-center justify-center" style={{ height: 500 }}>
        <div className="flex flex-col items-center gap-3 text-muted">
          <svg className="w-8 h-8 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
          </svg>
          <span className="text-sm">Loading OI data…</span>
        </div>
      </div>
    );
  }

  // ── no data yet ─────────────────────────────────────────────────────────
  if (!data) {
    return (
      <div className="panel flex items-center justify-center" style={{ height: 500 }}>
        <span className="text-muted text-sm">Waiting for data…</span>
      </div>
    );
  }

  if (data.rows.length === 0) {
    return (
      <div
        className="panel flex flex-col items-center justify-center gap-4 p-10 text-center border border-border"
        style={{ minHeight: 500 }}
      >
        <p className="text-base font-semibold text-foreground">No OI data in the database for this expiry</p>
        <p className="text-sm text-muted max-w-lg leading-relaxed">
          Charts read from ingested ticks stored in TimescaleDB. If nothing has been written yet, every strike shows empty.
          Live XTS feed ticks (and useful intraday OI change) usually appear only on{" "}
          <strong className="text-foreground">trading days, 9:15 AM – 3:30 PM IST</strong>. Weekends and holidays
          typically do not stream the same intraday option feed.
        </p>
        <ul className="text-xs text-muted text-left max-w-md space-y-2 list-disc pl-5">
          <li>
            Complete <strong className="text-foreground">Login &amp; Start Ingestion</strong> so the backend session is
            active.
          </li>
          <li>
            Keep <strong className="text-foreground">backend + Docker DB</strong> running and ensure{" "}
            <code className="text-accent">DB_URL</code> points at your Timescale container.
          </li>
          <li>
            Try again <strong className="text-foreground">during market hours</strong> for fresh snapshots; absolute OI
            may still be sparse right after first connect until ticks arrive.
          </li>
        </ul>
      </div>
    );
  }

  return (
    <div className="panel p-4 relative">
      {/* Loading overlay when switching timeframes (data still shown from previous fetch) */}
      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center z-10 bg-black/30 rounded-lg">
          <div className="flex items-center gap-2 bg-surface/90 px-4 py-2 rounded-full border border-border text-sm text-foreground">
            <svg className="w-4 h-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
            </svg>
            Fetching new timeframe…
          </div>
        </div>
      )}

      {/* "Market closed / no change" overlay inside the chart area */}
      {allChangeZero && (
        <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
          <div className="flex flex-col items-center gap-2 bg-slate-900/80 border border-yellow-500/30 rounded-xl px-6 py-4 text-center max-w-sm backdrop-blur-sm">
            <svg className="w-6 h-6 text-yellow-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6l4 2M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10z" />
            </svg>
            <p className="text-sm font-semibold text-yellow-300">No OI Change in This Window</p>
            <p className="text-xs text-slate-400 leading-relaxed">
              {nseSessionOpen === true ? (
                <>
                  The regular NSE session looks <span className="text-white font-medium">open</span> on the server clock, so
                  flat deltas usually mean unchanged OI versus the comparison time, a quiet broker feed, or snapshots not
                  flushing yet — check header <span className="text-white">XTS feed</span>, <span className="text-white">Last DB flush</span>, and try another timeframe.
                </>
              ) : nseSessionOpen === false ? (
                <>
                  Outside <span className="text-white font-medium">9:15 AM – 3:30 PM IST</span> (weekdays), OI is often
                  static in vendor feeds, so deltas stay at zero even though absolute OI is shown.
                </>
              ) : (
                <>
                  Either the comparison snapshot matches the latest tick, or the feed is not moving. After hours, brokers
                  often expose static OI; during <span className="text-white font-medium">9:15 AM – 3:30 PM IST</span> you
                  should see movement when the feed and DB are updating.
                </>
              )}
            </p>
            <p className="text-xs text-slate-500">Switch to <span className="text-white">OI (Absolute)</span> to see the OI distribution.</p>
          </div>
        </div>
      )}

      <ReactECharts
        option={option}
        notMerge
        lazyUpdate={false}
        style={{ height: 500, width: "100%", opacity: allChangeZero ? 0.3 : 1 }}
        opts={{ renderer: "canvas" }}
      />
    </div>
  );
}
