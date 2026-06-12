import type { OptionChainFullResponse } from "../types";
import { fmtFloat, fmtInt, fmtIvPct, fmtOi, fmtRatio } from "../utils/formatMarket";
import {
  partitionItm,
  partitionOtm,
  ratioSafe,
  totalBearishOi,
  totalBearishOiChg,
  totalBullishOi,
  totalBullishOiChg,
  totalsRowCore,
} from "../utils/netOiMath";
import { atmStrike, filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";

interface Props {
  data: OptionChainFullResponse | null;
  liveSpot: number | null;
  atmWindow: number;
  isLoading?: boolean;
}

function netClass(v: number): string {
  if (v > 0) return "text-pe";
  if (v < 0) return "text-ce";
  return "text-muted";
}

export function NetOICalculator({ data, liveSpot, atmWindow, isLoading }: Props) {
  const spot = liveSpot ?? data?.spot ?? null;
  const allRows = data?.rows ?? [];
  const windowRows = filterOiRowsByAtmWindow(allRows, spot, atmWindow);
  const strikes = allRows.map((r) => r.strike);
  const atm = spot != null && strikes.length ? atmStrike(strikes, spot) : null;

  const core = totalsRowCore(windowRows);

  const t1rows = [
    {
      stat: "Total OI",
      c: core.totalCallOi,
      p: core.totalPutOi,
      n: core.totalPutOi - core.totalCallOi,
    },
    {
      stat: "Total OI Chg",
      c: core.totalCallChg,
      p: core.totalPutChg,
      n: core.totalPutChg - core.totalCallChg,
    },
    {
      stat: "Total Volume",
      c: core.totalCallVol,
      p: core.totalPutVol,
      n: core.totalPutVol - core.totalCallVol,
    },
  ];

  const pcrOi = ratioSafe(core.totalPutOi, core.totalCallOi);
  const pcrOiChg = ratioSafe(core.totalPutChg, core.totalCallChg);
  const pcrVol = ratioSafe(core.totalPutVol, core.totalCallVol);

  const t2rows: { stat: string; val: number | null; kind: "int" | "float" | "ratio" }[] = [
    { stat: "Total Bullish OI", val: totalBullishOi(windowRows), kind: "int" },
    { stat: "Total Bearish OI", val: totalBearishOi(windowRows), kind: "int" },
    { stat: "Total Bullish OI Chg", val: totalBullishOiChg(windowRows), kind: "int" },
    { stat: "Total Bearish OI Chg", val: totalBearishOiChg(windowRows), kind: "int" },
    { stat: "Total Calls Premium", val: core.totalCallLtp, kind: "float" },
    { stat: "Total Puts Premium", val: core.totalPutLtp, kind: "float" },
    { stat: "Tot Call Premium Chg", val: core.totalCallLtpChg, kind: "float" },
    { stat: "Tot Put Premium Chg", val: core.totalPutLtpChg, kind: "float" },
    { stat: "PE-CE OI Chg", val: core.totalPutChg - core.totalCallChg, kind: "int" },
    { stat: "PCR OI", val: pcrOi, kind: "ratio" },
    { stat: "PCR OI Chg", val: pcrOiChg, kind: "ratio" },
    { stat: "PCR Volume", val: pcrVol, kind: "ratio" },
  ];

  let otmBlock = null as ReturnType<typeof partitionOtm> | null;
  let itmBlock = null as ReturnType<typeof partitionItm> | null;
  if (spot != null) {
    otmBlock = partitionOtm(windowRows, spot);
    itmBlock = partitionItm(windowRows, spot);
  }

  const otmPcrOi = otmBlock ? ratioSafe(otmBlock.putOi, otmBlock.callOi) : null;
  const otmPcrChg = otmBlock ? ratioSafe(otmBlock.putOiChg, otmBlock.callOiChg) : null;
  const otmPcrVol = otmBlock ? ratioSafe(otmBlock.putVol, otmBlock.callVol) : null;
  const itmPcrOi = itmBlock ? ratioSafe(itmBlock.putOi, itmBlock.callOi) : null;
  const itmPcrChg = itmBlock ? ratioSafe(itmBlock.putOiChg, itmBlock.callOiChg) : null;
  const itmPcrVol = itmBlock ? ratioSafe(itmBlock.putVol, itmBlock.callVol) : null;

  if (isLoading && !data) {
    return <div className="panel p-8 text-center text-muted text-sm">Loading net OI calculator…</div>;
  }

  /** Strike ladder: grid + thick white section dividers (Calls | middle | Puts) */
  const grid = "border border-border/80";
  const thc = `py-1 px-1 text-[11px] leading-tight font-mono uppercase tracking-tight align-middle ${grid}`;
  const tc = `py-0.5 px-1 text-sm leading-snug font-mono tabular-nums align-middle ${grid}`;
  const callBg = "bg-green-950/25";
  /** PE−CE + PCR columns (amber band) */
  const midMetricsBg = "bg-amber-950/30";
  /** Strike price column only — distinct from metrics */
  const strikeColBg = "bg-violet-950/55";
  const strikeColHeaderBg = "bg-violet-900/60";
  const putBg = "bg-red-950/25";
  /** Thick white line between major sections */
  const sectionDividerR = "border-r-[5px] border-r-white";
  /** White line between Strike column and PE−CE / PCR */
  const strikeMidDividerR = "border-r-2 border-r-white";
  /** Full-row highlight for ATM strike */
  const atmRowBg = "bg-yellow-400/45";
  /** Summary panels — full cell grid */
  const sumCell = "border border-border/70 align-middle";
  const st = `py-0.5 px-1 text-sm leading-tight font-mono tabular-nums ${sumCell}`;
  const sth = `py-1 px-1 text-xs leading-tight text-muted ${sumCell} bg-[#0c1220]/95`;

  return (
    <div className="flex flex-col gap-2">
      {/* Strike table */}
      <div className="panel overflow-hidden">
        <div className="px-2 py-1.5 border-b border-border flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0">
            <h3 className="text-base font-semibold text-gray-100 shrink-0">Net Calculation OI — Strike ladder</h3>
            {data && (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <div
                  className="rounded border border-amber-500/40 bg-amber-950/40 px-2 py-0.5 flex items-baseline gap-1.5"
                  title="Put OI ÷ Call OI for strikes in current window"
                >
                  <span className="text-[10px] text-amber-200/90 uppercase tracking-wide">PCR OI</span>
                  <span className="text-lg font-bold text-amber-100 tabular-nums">{fmtRatio(pcrOi)}</span>
                </div>
                <div
                  className="rounded border border-yellow-500/35 bg-yellow-950/25 px-2 py-0.5 flex items-baseline gap-1.5"
                  title="Put volume ÷ Call volume for strikes in current window"
                >
                  <span className="text-[10px] text-yellow-200/90 uppercase tracking-wide">PCR Vol</span>
                  <span className="text-lg font-bold text-yellow-100 tabular-nums">{fmtRatio(pcrVol)}</span>
                </div>
              </div>
            )}
          </div>
          {data && (
            <span className="text-xs text-muted font-mono whitespace-nowrap">
              {atmWindow < 0 ? "All strikes" : atmWindow === 0 ? "ATM only" : `ATM ±${atmWindow}`} · TF {data.timeframe} · Lot{" "}
              {data.lot_size}
            </span>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-[1380px] w-full border-collapse border border-border bg-[#0a0f18]">
            <thead>
              <tr>
                <th
                  colSpan={6}
                  className={`${thc} bg-green-900/50 text-green-200 text-center font-semibold tracking-wide ${sectionDividerR}`}
                >
                  Calls
                </th>
                <th
                  colSpan={1}
                  className={`${thc} ${strikeColHeaderBg} text-violet-100 text-center font-semibold tracking-wide ${strikeMidDividerR}`}
                >
                  Strike
                </th>
                <th
                  colSpan={4}
                  className={`${thc} bg-amber-900/45 text-amber-100 text-center font-semibold tracking-wide ${sectionDividerR}`}
                >
                  PE−CE / PCR
                </th>
                <th
                  colSpan={5}
                  className={`${thc} bg-red-900/50 text-red-200 text-center font-semibold tracking-wide`}
                >
                  Puts
                </th>
              </tr>
              <tr className="text-muted">
                <th className={`${thc} ${callBg} text-right text-green-200/90`}>IV</th>
                <th className={`${thc} ${callBg} text-right text-green-200/90`}>OI Chg</th>
                <th className={`${thc} ${callBg} text-right text-green-200/90`}>OI</th>
                <th className={`${thc} ${callBg} text-right text-green-200/90`}>Vol</th>
                <th className={`${thc} ${callBg} text-right text-green-200/90`}>LTP Chg</th>
                <th className={`${thc} ${callBg} text-right text-green-200/90 ${sectionDividerR}`}>LTP</th>
                <th className={`${thc} ${strikeColHeaderBg} text-center text-violet-100 ${strikeMidDividerR}`}>
                  Strike
                </th>
                <th className={`${thc} ${midMetricsBg} text-right text-amber-100`}>PE−CE OI</th>
                <th className={`${thc} ${midMetricsBg} text-right text-amber-100`}>PE−CE Chg</th>
                <th className={`${thc} ${midMetricsBg} text-right text-amber-100`}>PCR OI</th>
                <th className={`${thc} ${midMetricsBg} text-right text-amber-100 ${sectionDividerR}`}>PCR Vol</th>
                <th className={`${thc} ${putBg} text-right text-red-200/90`}>LTP</th>
                <th className={`${thc} ${putBg} text-right text-red-200/90`}>Vol</th>
                <th className={`${thc} ${putBg} text-right text-red-200/90`}>OI</th>
                <th className={`${thc} ${putBg} text-right text-red-200/90`}>OI Chg</th>
                <th className={`${thc} ${putBg} text-right text-red-200/90`}>IV</th>
              </tr>
            </thead>
            <tbody>
              {windowRows.map((r, idx) => {
                const isAtm = atm != null && r.strike === atm;
                const zebra = !isAtm && idx % 2 === 1 ? "bg-black/25" : "";
                const cBg = isAtm ? atmRowBg : callBg;
                const sBg = isAtm ? atmRowBg : strikeColBg;
                const mBg = isAtm ? atmRowBg : midMetricsBg;
                const pBg = isAtm ? atmRowBg : putBg;
                return (
                  <tr
                    key={r.strike}
                    className={`${zebra} ${isAtm ? "ring-2 ring-inset ring-yellow-500/80" : ""}`}
                  >
                    <td className={`${tc} ${cBg} text-right text-violet-300/90`}>{fmtIvPct(r.call_iv)}</td>
                    <td
                      className={`${tc} ${cBg} text-right ${
                        r.call_oi_change >= 0 ? "text-red-300/90" : "text-green-300/90"
                      }`}
                    >
                      {fmtInt(r.call_oi_change)}
                    </td>
                    <td className={`${tc} ${cBg} text-right text-ce/90`}>{fmtOi(r.call_oi)}</td>
                    <td className={`${tc} ${cBg} text-right text-sky-300/80`}>{fmtOi(r.call_volume)}</td>
                    <td className={`${tc} ${cBg} text-right text-gray-200`}>{fmtFloat(r.call_ltp_change)}</td>
                    <td className={`${tc} ${cBg} text-right text-sky-200 ${sectionDividerR}`}>
                      {fmtFloat(r.call_ltp)}
                    </td>
                    <td
                      className={`${tc} ${sBg} text-center font-semibold ${strikeMidDividerR} ${
                        isAtm ? "text-yellow-950" : "text-violet-50"
                      }`}
                    >
                      {r.strike}
                    </td>
                    <td className={`${tc} ${mBg} text-right ${netClass(r.pe_ce_oi)}`}>{fmtInt(r.pe_ce_oi)}</td>
                    <td className={`${tc} ${mBg} text-right ${netClass(r.pe_ce_oi_change)}`}>
                      {fmtInt(r.pe_ce_oi_change)}
                    </td>
                    <td
                      className={`${tc} ${mBg} text-right font-semibold ${
                        isAtm ? "text-amber-900" : "text-yellow-300/95"
                      }`}
                    >
                      {fmtRatio(r.pcr_oi)}
                    </td>
                    <td
                      className={`${tc} ${mBg} text-right font-semibold ${sectionDividerR} ${
                        isAtm ? "text-amber-900" : "text-yellow-200/95"
                      }`}
                    >
                      {fmtRatio(r.pcr_volume)}
                    </td>
                    <td className={`${tc} ${pBg} text-right text-sky-200`}>{fmtFloat(r.put_ltp)}</td>
                    <td className={`${tc} ${pBg} text-right text-sky-300/80`}>{fmtOi(r.put_volume)}</td>
                    <td className={`${tc} ${pBg} text-right text-pe/90`}>{fmtOi(r.put_oi)}</td>
                    <td
                      className={`${tc} ${pBg} text-right ${
                        r.put_oi_change >= 0 ? "text-green-300/90" : "text-red-300/90"
                      }`}
                    >
                      {fmtInt(r.put_oi_change)}
                    </td>
                    <td className={`${tc} ${pBg} text-right text-violet-300/90`}>{fmtIvPct(r.put_iv)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Summary tables */}
      <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-4 gap-2">
        <div className="panel p-2 overflow-x-auto">
          <h4 className="text-xs font-semibold text-gray-300 mb-1 uppercase tracking-wide">Totals</h4>
          <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14]">
            <thead>
              <tr className="text-muted">
                <th className={`${sth} text-left`}>Stat</th>
                <th className={`${sth} text-right text-ce/80`}>Calls</th>
                <th className={`${sth} text-right text-pe/80`}>Puts</th>
                <th className={`${sth} text-right`}>Net</th>
              </tr>
            </thead>
            <tbody>
              {t1rows.map((row) => (
                <tr key={row.stat}>
                  <td className={`${st} text-muted`}>{row.stat}</td>
                  <td className={`${st} text-right text-ce/90`}>{fmtInt(row.c)}</td>
                  <td className={`${st} text-right text-pe/90`}>{fmtInt(row.p)}</td>
                  <td className={`${st} text-right ${netClass(row.n)}`}>{fmtInt(row.n)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel p-2 overflow-x-auto">
          <h4 className="text-xs font-semibold text-gray-300 mb-1 uppercase tracking-wide">Totals — Stats</h4>
          <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14]">
            <thead>
              <tr className="text-muted">
                <th className={`${sth} text-left`}>Stat</th>
                <th className={`${sth} text-right`}>Value</th>
              </tr>
            </thead>
            <tbody>
              {t2rows.map((row) => (
                <tr key={row.stat}>
                  <td className={`${st} text-muted`}>{row.stat}</td>
                  <td
                    className={`${st} text-right ${
                      row.kind === "ratio"
                        ? "text-yellow-300/90"
                        : row.kind === "int"
                          ? netClass(row.val ?? 0)
                          : "text-gray-200"
                    }`}
                  >
                    {row.kind === "ratio"
                      ? fmtRatio(row.val)
                      : row.kind === "float"
                        ? fmtFloat(row.val, 2)
                        : fmtInt(Math.round(row.val ?? 0))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel p-2 overflow-x-auto">
          <h4 className="text-xs font-semibold text-gray-300 mb-1 uppercase tracking-wide">OTM</h4>
          {spot == null || !otmBlock ? (
            <p className="text-xs text-muted">Set spot to compute OTM / ITM.</p>
          ) : (
            <>
              <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14] mb-1">
                <thead>
                  <tr className="text-muted">
                    <th className={`${sth} text-left`}>Stat</th>
                    <th className={`${sth} text-right text-ce/80`}>Calls</th>
                    <th className={`${sth} text-right text-pe/80`}>Puts</th>
                    <th className={`${sth} text-right`}>Net</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className={`${st} text-muted`}>Total OTM OI</td>
                    <td className={`${st} text-right text-ce/90`}>{fmtInt(otmBlock.callOi)}</td>
                    <td className={`${st} text-right text-pe/90`}>{fmtInt(otmBlock.putOi)}</td>
                    <td className={`${st} text-right ${netClass(otmBlock.putOi - otmBlock.callOi)}`}>
                      {fmtInt(otmBlock.putOi - otmBlock.callOi)}
                    </td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>Total OTM OI Chg</td>
                    <td className={`${st} text-right`}>{fmtInt(otmBlock.callOiChg)}</td>
                    <td className={`${st} text-right`}>{fmtInt(otmBlock.putOiChg)}</td>
                    <td className={`${st} text-right ${netClass(otmBlock.putOiChg - otmBlock.callOiChg)}`}>
                      {fmtInt(otmBlock.putOiChg - otmBlock.callOiChg)}
                    </td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>Total OTM Volume</td>
                    <td className={`${st} text-right`}>{fmtInt(otmBlock.callVol)}</td>
                    <td className={`${st} text-right`}>{fmtInt(otmBlock.putVol)}</td>
                    <td className={`${st} text-right ${netClass(otmBlock.putVol - otmBlock.callVol)}`}>
                      {fmtInt(otmBlock.putVol - otmBlock.callVol)}
                    </td>
                  </tr>
                </tbody>
              </table>
              <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14]">
                <thead>
                  <tr>
                    <th className={`${sth} text-left text-muted`}>PCR (OTM)</th>
                    <th className={`${sth} text-right text-yellow-200/80`}>Value</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className={`${st} text-muted`}>PCR OTM OI</td>
                    <td className={`${st} text-right text-yellow-300/90`}>{fmtRatio(otmPcrOi)}</td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>PCR OTM OI Chg</td>
                    <td className={`${st} text-right text-yellow-300/90`}>{fmtRatio(otmPcrChg)}</td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>PCR OTM Vol</td>
                    <td className={`${st} text-right text-yellow-200/90`}>{fmtRatio(otmPcrVol)}</td>
                  </tr>
                </tbody>
              </table>
            </>
          )}
        </div>

        <div className="panel p-2 overflow-x-auto">
          <h4 className="text-xs font-semibold text-gray-300 mb-1 uppercase tracking-wide">ITM</h4>
          {spot == null || !itmBlock ? (
            <p className="text-xs text-muted">Set spot to compute OTM / ITM.</p>
          ) : (
            <>
              <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14] mb-1">
                <thead>
                  <tr className="text-muted">
                    <th className={`${sth} text-left`}>Stat</th>
                    <th className={`${sth} text-right text-ce/80`}>Calls</th>
                    <th className={`${sth} text-right text-pe/80`}>Puts</th>
                    <th className={`${sth} text-right`}>Net</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className={`${st} text-muted`}>Total ITM OI</td>
                    <td className={`${st} text-right text-ce/90`}>{fmtInt(itmBlock.callOi)}</td>
                    <td className={`${st} text-right text-pe/90`}>{fmtInt(itmBlock.putOi)}</td>
                    <td className={`${st} text-right ${netClass(itmBlock.putOi - itmBlock.callOi)}`}>
                      {fmtInt(itmBlock.putOi - itmBlock.callOi)}
                    </td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>Total ITM OI Chg</td>
                    <td className={`${st} text-right`}>{fmtInt(itmBlock.callOiChg)}</td>
                    <td className={`${st} text-right`}>{fmtInt(itmBlock.putOiChg)}</td>
                    <td className={`${st} text-right ${netClass(itmBlock.putOiChg - itmBlock.callOiChg)}`}>
                      {fmtInt(itmBlock.putOiChg - itmBlock.callOiChg)}
                    </td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>Total ITM Volume</td>
                    <td className={`${st} text-right`}>{fmtInt(itmBlock.callVol)}</td>
                    <td className={`${st} text-right`}>{fmtInt(itmBlock.putVol)}</td>
                    <td className={`${st} text-right ${netClass(itmBlock.putVol - itmBlock.callVol)}`}>
                      {fmtInt(itmBlock.putVol - itmBlock.callVol)}
                    </td>
                  </tr>
                </tbody>
              </table>
              <table className="w-full font-mono border-collapse border border-border/80 bg-[#080c14]">
                <thead>
                  <tr>
                    <th className={`${sth} text-left text-muted`}>PCR (ITM)</th>
                    <th className={`${sth} text-right text-yellow-200/80`}>Value</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className={`${st} text-muted`}>PCR ITM OI</td>
                    <td className={`${st} text-right text-yellow-300/90`}>{fmtRatio(itmPcrOi)}</td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>PCR ITM OI Chg</td>
                    <td className={`${st} text-right text-yellow-300/90`}>{fmtRatio(itmPcrChg)}</td>
                  </tr>
                  <tr>
                    <td className={`${st} text-muted`}>PCR ITM Vol</td>
                    <td className={`${st} text-right text-yellow-200/90`}>{fmtRatio(itmPcrVol)}</td>
                  </tr>
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>

      {!windowRows.length && !isLoading && (
        <div className="panel p-4 text-center text-muted text-sm">No rows in ATM window.</div>
      )}
    </div>
  );
}
