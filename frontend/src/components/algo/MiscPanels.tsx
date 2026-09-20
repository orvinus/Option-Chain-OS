/**
 * Smaller Algo Config sub-tabs that are pure config editors:
 * Holiday Calendar, Integrations & Broker (connection status + fee model),
 * Paper Trading settings, and the P&L placeholder until the simulator
 * milestone wires real ledgers.
 */
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { AlgoApiError, algoApi } from "../../api/algoRest";
import type { AlgoConfigDoc, BrokerAccount, PaperSession, TelegramStatus } from "../../types/algo";
import { CalcNote, Card, DateField, NumField, ToggleLine } from "./controls";
import { trackRequest } from "../../api/inflight";
import { useAlgoStreamContext } from "../../hooks/useAlgoStream";
import { inr2, num2, pnlTone, positionLabel, streamPosition } from "./streamPosition";

/** Flatten the gateway's nested balance structure into label/value rows so
 *  EVERY field the broker reports is visible, whatever its exact shape. */
function flattenAccount(obj: unknown, prefix = ""): [string, string][] {
  if (obj === null || obj === undefined || obj === "") return [];
  if (Array.isArray(obj)) {
    return obj.flatMap((v, i) => flattenAccount(v, prefix ? `${prefix}[${i}]` : `[${i}]`));
  }
  if (typeof obj === "object") {
    return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) =>
      flattenAccount(v, prefix ? `${prefix}.${k}` : k)
    );
  }
  return [[prefix, String(obj)]];
}

const BALANCE_HIGHLIGHTS: [RegExp, string][] = [
  [/cashavailable/i, "Cash available"],
  [/netmarginavailable/i, "Net margin available"],
  [/marginutilized/i, "Margin utilized"],
  [/collateral/i, "Collateral"],
];

interface PanelProps {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
}

// ── Holiday Calendar ──────────────────────────────────────────────────────

export function HolidaysPanel({ draft, mutate }: PanelProps) {
  const [date, setDate] = useState("");
  const [occasion, setOccasion] = useState("");
  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Card title="Exchange Holidays" hint="engine auto-skips these dates entirely">
        {draft.global.holidays.length === 0 && (
          <div className="text-xs text-muted">No holidays configured.</div>
        )}
        {draft.global.holidays.map((h, i) => (
          <div
            key={`${h.date}-${i}`}
            className="flex items-center justify-between py-1.5 border-b border-border/40 last:border-b-0 text-sm"
          >
            <div>
              <span className="text-gray-100 font-mono">{h.date}</span>
              <span className="ml-3 text-muted text-xs">{h.occasion}</span>
            </div>
            <button
              type="button"
              className="text-ce text-xs hover:text-white"
              onClick={() =>
                mutate((doc) => void doc.global.holidays.splice(i, 1))
              }
            >
              ✕ remove
            </button>
          </div>
        ))}
      </Card>
      <Card title="Add Holiday">
        <div className="grid grid-cols-2 gap-3 mb-3">
          <DateField label="Date" value={date} onChange={setDate} />
          <label className="block">
            <span className="block text-[10px] text-muted mb-1">Occasion</span>
            <input
              className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm w-full focus:outline-none focus:border-accent"
              placeholder="e.g. Diwali Balipratipada"
              value={occasion}
              onChange={(e) => setOccasion(e.target.value)}
            />
          </label>
        </div>
        <button
          type="button"
          className="pill pill-active disabled:opacity-50"
          disabled={!date}
          onClick={() => {
            mutate((doc) => void doc.global.holidays.push({ date, occasion }));
            setDate("");
            setOccasion("");
          }}
        >
          Add to calendar
        </button>
        <div className="mt-3">
          <CalcNote>
            A listed date acts as an automatic day-level kill switch driven by the calendar. Save
            the config to make additions live.
          </CalcNote>
        </div>
      </Card>
    </div>
  );
}

// ── Integrations & Broker ─────────────────────────────────────────────────

interface BrokerStatus {
  mode: "paper" | "live";
  detail: string;
  configured: boolean;
  logged_in: boolean;
  last_error: string;
}

export function IntegrationsPanel({ draft, mutate }: PanelProps) {
  const f = draft.global.fees;
  const [broker, setBroker] = useState<BrokerStatus | null>(null);
  const [probeMsg, setProbeMsg] = useState<string | null>(null);
  const [reconMsg, setReconMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        // Wrapped so this raw fetch is counted like every algoApi call.
        const res = await trackRequest(() =>
          fetch("/api/algo/broker/status", { credentials: "same-origin" }),
        );
        if (res.ok && !cancelled) setBroker((await res.json()) as BrokerStatus);
      } catch {
        /* panel stays in the unknown state */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Card title="Connected Systems">
        <ConnRow name="OI Algo — OI Data Feed" meta="this platform's own ingestion" ok />
        <ConnRow
          name="Lakshmishree Broking — Order Execution"
          meta={broker ? broker.detail : "checking…"}
          ok={broker?.configured ?? false}
        />
        {broker?.configured && (
          <div className="flex items-center gap-2 py-1.5">
            <button
              type="button"
              className="pill text-xs"
              onClick={() =>
                void (async () => {
                  setProbeMsg("probing…");
                  try {
                    const res = await fetch("/api/algo/broker/test-login", {
                      method: "POST",
                      credentials: "same-origin",
                    });
                    const body = (await res.json()) as { detail?: string };
                    setProbeMsg(res.ok ? "✔ Interactive session OK" : `✗ ${body.detail ?? res.status}`);
                  } catch (e) {
                    setProbeMsg(`✗ ${e instanceof Error ? e.message : String(e)}`);
                  }
                })()
              }
            >
              Test Interactive login
            </button>
            {probeMsg && <span className="text-[11px] text-muted">{probeMsg}</span>}
          </div>
        )}
        {broker?.configured && (
          <div className="flex items-center gap-2 py-1.5">
            <button
              type="button"
              className="pill text-xs"
              onClick={() =>
                void (async () => {
                  setReconMsg("checking…");
                  try {
                    const res = await fetch("/api/algo/broker/reconcile", {
                      credentials: "same-origin",
                    });
                    const body = (await res.json()) as {
                      match: boolean | null;
                      detail: string;
                    };
                    setReconMsg(
                      res.ok
                        ? `${body.match === false ? "✗ MISMATCH — " : body.match ? "✔ " : ""}${body.detail}`
                        : `✗ ${res.status}`
                    );
                  } catch (e) {
                    setReconMsg(`✗ ${e instanceof Error ? e.message : String(e)}`);
                  }
                })()
              }
            >
              Reconcile positions
            </button>
            {reconMsg && <span className="text-[11px] text-muted">{reconMsg}</span>}
          </div>
        )}
        <TelegramRow />
        <div className="mt-2">
          <CalcNote>
            Auto-failover to a backup broker stays disabled until a backup is actually connected —
            never silently fail over to nothing.
          </CalcNote>
        </div>
      </Card>
      <Card
        title="Brokerage & Statutory Charges"
        hint="seeded with Lakshmishree's real rates — fully editable"
      >
        <div className="grid grid-cols-2 gap-3">
          <NumField
            label="Brokerage per executed order (₹)"
            value={f.brokerage_per_order}
            onChange={(v) => mutate((d) => void (d.global.fees.brokerage_per_order = v))}
          />
          <NumField
            label="STT — sell-side premium (%)"
            value={f.stt_sell_premium_pct}
            step={0.001}
            onChange={(v) => mutate((d) => void (d.global.fees.stt_sell_premium_pct = v))}
          />
          <NumField
            label="Exchange transaction (%)"
            value={f.exchange_txn_pct}
            step={0.0001}
            onChange={(v) => mutate((d) => void (d.global.fees.exchange_txn_pct = v))}
          />
          <NumField
            label="SEBI turnover (%)"
            value={f.sebi_turnover_pct}
            step={0.0001}
            onChange={(v) => mutate((d) => void (d.global.fees.sebi_turnover_pct = v))}
          />
          <NumField
            label="IPFT (%)"
            value={f.ipft_pct}
            step={0.0001}
            onChange={(v) => mutate((d) => void (d.global.fees.ipft_pct = v))}
          />
          <NumField
            label="GST on charges (%)"
            value={f.gst_pct}
            step={0.1}
            onChange={(v) => mutate((d) => void (d.global.fees.gst_pct = v))}
          />
          <NumField
            label="Stamp duty — buy side (%)"
            value={f.stamp_duty_buy_pct}
            step={0.001}
            onChange={(v) => mutate((d) => void (d.global.fees.stamp_duty_buy_pct = v))}
          />
        </div>
        <div className="mt-2">
          <CalcNote>
            Used identically by the paper simulator and live P&L, so both always speak the same
            costs. Lakshmishree charges a flat ₹17 per executed F&O order.
          </CalcNote>
        </div>
      </Card>
      <div className="md:col-span-2" id="manual-order-test">
        <ManualOrderTestCard paperMode={draft.global.paper.paper_mode} />
      </div>
      {broker?.configured && (
        <div className="md:col-span-2">
          <LiveAccountCard />
        </div>
      )}
    </div>
  );
}

type ManualEntryResult = {
  trade_id: number; ledger: string; symbol: string; expiry: string | null;
  strike: number; option_type: string; side: string; premium_at_decision: number;
  fill_price: number; lots: number; lot_size: number; quantity: number; notional: number;
  broker_order_id: string; broker_status: string; zone_id: string; recorded_error: string;
  timings_ms: Record<string, number>; server_ms: number;
};
type ManualExitResult = {
  trade_id: number; ledger: string; closed: boolean; exit_reason: string;
  reference_price: number; reference_source?: string; reference_ts?: string | null;
  message: string; timings_ms: Record<string, number>;
  server_ms: number;
  trade: { exit_price: number | null; pnl_rupees: number | null; exit_reason: string | null } | null;
};

/**
 * Burn-in tool for the live broker: one capped test entry and a manual
 * square-off, both through the engine's OWN execution and exit code. The
 * server caps the notional at ₹1,000 and demands confirmation when Paper Mode
 * is off, so a slip here can never take real exposure.
 */
function ManualOrderTestCard({ paperMode }: { paperMode: boolean }) {
  const [side, setSide] = useState<"CALL" | "PUT">("CALL");
  const [maxNotional, setMaxNotional] = useState(1000);
  const [pMin, setPMin] = useState(0.5);
  const [pMax, setPMax] = useState(2);
  const [busy, setBusy] = useState<"" | "entry" | "exit">("");
  const [entry, setEntry] = useState<(ManualEntryResult & { client_ms: number }) | null>(null);
  const [exit, setExit] = useState<(ManualExitResult & { client_ms: number }) | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const post = async <T,>(url: string, body: unknown): Promise<{ data: T; ms: number }> => {
    const t0 = performance.now();
    const res = await trackRequest(() =>
      fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
    const ms = Math.round(performance.now() - t0);
    const data = (await res.json().catch(() => ({}))) as T & { detail?: unknown };
    if (!res.ok) {
      const d = (data as { detail?: unknown }).detail;
      throw new Error(typeof d === "string" ? d : JSON.stringify(d ?? res.status));
    }
    return { data, ms };
  };

  const placeEntry = async () => {
    const mode = paperMode ? "PAPER (simulated)" : "LIVE — a REAL order at the broker";
    if (!window.confirm(
      `Place a ${mode} test ${side} entry?\n\nPremium band ₹${pMin}–₹${pMax}, max ₹${maxNotional} notional.`,
    )) return;
    setBusy("entry"); setErr(null); setExit(null);
    try {
      const { data, ms } = await post<ManualEntryResult>("/api/algo/manual/test-entry", {
        side, max_notional: maxNotional, premium_min: pMin, premium_max: pMax, confirm_live: true,
      });
      setEntry({ ...data, client_ms: ms });
    } catch (e) {
      setErr(`Entry refused: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy("");
    }
  };

  const squareOff = async () => {
    if (!window.confirm("Square off the open position now?\n\nA LIVE position sends a REAL market sell.")) return;
    setBusy("exit"); setErr(null);
    try {
      const { data, ms } = await post<ManualExitResult>("/api/algo/manual/square-off", { confirm_live: true });
      setExit({ ...data, client_ms: ms });
    } catch (e) {
      setErr(`Square-off refused: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy("");
    }
  };

  const row = (k: string, v: ReactNode) => (
    <div className="flex justify-between gap-3 py-0.5 border-b border-border/30 last:border-b-0">
      <span className="text-muted">{k}</span>
      <span className="font-mono text-gray-200 text-right">{v}</span>
    </div>
  );
  const timings = (t: Record<string, number>) =>
    Object.entries(t).map(([k, v]) => `${k.replace(/_/g, " ")} ${v} ms`).join(" · ");

  return (
    <Card
      title="Manual order test"
      hint={paperMode ? "Paper Mode ON — simulated fills" : "Paper Mode OFF — REAL broker orders"}
    >
      <div
        className={`mb-3 rounded-lg border px-3 py-2 text-[11px] ${
          paperMode ? "border-border/60 text-muted" : "border-ce/60 bg-ce/10 text-ce"
        }`}
      >
        {paperMode
          ? "Paper Mode is ON: these buttons use the paper simulator, exactly like an engine entry."
          : "LIVE: these buttons place REAL orders through Lakshmishree. The server caps a test entry at ₹1,000 notional."}
        {" "}Both use the engine's own order and exit code; the position is then managed like any other
        (Square off, broker reconcile, 15:25 expiry close).
      </div>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 items-end">
        <label className="flex flex-col gap-1 text-[10.5px] text-muted">
          Side
          <select
            className="input text-xs"
            value={side}
            onChange={(e) => setSide(e.target.value as "CALL" | "PUT")}
            disabled={busy !== ""}
          >
            <option value="CALL">BUY CALL (CE)</option>
            <option value="PUT">BUY PUT (PE)</option>
          </select>
        </label>
        <NumField label="Premium min (₹)" value={pMin} step={0.05} onChange={setPMin} />
        <NumField label="Premium max (₹)" value={pMax} step={0.05} onChange={setPMax} />
        <NumField label="Max notional (₹, ≤ 1000)" value={maxNotional} step={50} onChange={(v) => setMaxNotional(Math.min(1000, v))} />
        <div className="flex gap-2">
          <button
            type="button"
            className="pill pill-active text-xs disabled:opacity-40"
            disabled={busy !== ""}
            onClick={() => void placeEntry()}
          >
            {busy === "entry" ? "Placing…" : "Place test entry"}
          </button>
          <button
            type="button"
            className="pill text-xs border-ce/60 text-ce disabled:opacity-40"
            disabled={busy !== ""}
            onClick={() => void squareOff()}
          >
            {busy === "exit" ? "Squaring off…" : "Square off"}
          </button>
        </div>
      </div>
      {err && (
        <div className="mt-3 rounded-lg border border-ce/40 bg-ce/10 px-3 py-2 text-[11px] text-ce">{err}</div>
      )}
      <div className="mt-3 grid md:grid-cols-2 gap-4 text-[11px]">
        {entry && (
          <div data-testid="manual-entry-result">
            <div className="text-[11px] font-semibold text-pe mb-1">
              ✓ ENTRY FILLED — trade #{entry.trade_id} [{entry.ledger}]
            </div>
            {row("Contract", `${entry.symbol} ${entry.strike} ${entry.option_type} · exp ${entry.expiry}`)}
            {row("Premium at decision", `₹${entry.premium_at_decision.toFixed(2)}`)}
            {row("Fill price", `₹${entry.fill_price.toFixed(2)}`)}
            {row("Lots × lot size = qty", `${entry.lots} × ${entry.lot_size} = ${entry.quantity}`)}
            {row("Notional", `₹${entry.notional.toFixed(2)}`)}
            {row("Broker order id", entry.broker_order_id || "— (paper)")}
            {row("Broker status", entry.broker_status || "—")}
            {row("Zone", entry.zone_id)}
            {row("Latency: click → response", `${entry.client_ms} ms`)}
            {row("Latency: server total", `${entry.server_ms} ms`)}
            <div className="mt-1 text-muted">{timings(entry.timings_ms)}</div>
            {entry.recorded_error && <div className="mt-1 text-ce">ledger: {entry.recorded_error}</div>}
          </div>
        )}
        {exit && (
          <div data-testid="manual-exit-result">
            <div className={`text-[11px] font-semibold mb-1 ${exit.closed ? "text-pe" : "text-ce"}`}>
              {exit.closed ? "✓ POSITION CLOSED" : "✗ STILL OPEN"} — trade #{exit.trade_id} [{exit.ledger}]
            </div>
            {row("Exit reason", exit.trade?.exit_reason ?? exit.exit_reason)}
            {row(
              "Last traded price before the sell",
              `${inr2(exit.reference_price)}${exit.reference_ts ? ` @ ${exit.reference_ts.slice(11, 19)}` : ""}${
                exit.reference_source && exit.reference_source !== "last traded price" ? ` (${exit.reference_source})` : ""
              }`,
            )}
            {row("Exit fill (broker)", exit.trade?.exit_price != null ? inr2(exit.trade.exit_price) : "—")}
            {exit.trade?.exit_price != null &&
              row(
                "Slippage vs last price",
                `${inr2(exit.trade.exit_price - exit.reference_price, true)} per unit`,
              )}
            {row("Net P&L (after charges)", exit.trade?.pnl_rupees != null ? `₹${exit.trade.pnl_rupees.toFixed(2)}` : "—")}
            {row("Latency: click → response", `${exit.client_ms} ms`)}
            {row("Latency: server total", `${exit.server_ms} ms`)}
            <div className="mt-1 text-muted">{timings(exit.timings_ms)}</div>
            {!exit.closed && <div className="mt-1 text-ce">{exit.message}</div>}
          </div>
        )}
      </div>
    </Card>
  );
}

/** A-to-Z live broker account: balance, net positions, today's orders. */
function LiveAccountCard() {
  const [acct, setAcct] = useState<BrokerAccount | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setAcct(await algoApi.brokerAccount());
      setErr(null);
    } catch (e) {
      setAcct(null);
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // When the gateway is offline (weekends/off-hours) fall back to the
  // last-GOOD snapshot the backend cached, clearly labelled as stale.
  const snap = acct && !acct.connected ? acct.last_snapshot ?? null : null;
  const eff = acct?.connected
    ? { balance: acct.balance, positions: acct.positions, orders: acct.orders }
    : snap
      ? (snap.payload as {
          balance: unknown;
          positions: Record<string, unknown>[];
          orders: Record<string, unknown>[];
        })
      : null;

  const balanceRows = eff?.balance ? flattenAccount(eff.balance) : [];
  const highlights = BALANCE_HIGHLIGHTS.map(([re, label]) => {
    const hit = balanceRows.find(([k]) => re.test(k));
    return hit ? ([label, hit[1]] as const) : null;
  }).filter((x): x is readonly [string, string] => x !== null);

  const posCols = ["TradingSymbol", "ExchangeInstrumentId", "ProductType", "Quantity",
    "BuyAveragePrice", "SellAveragePrice", "OurLTP", "OurMTM", "RealizedMTM", "MTM", "UnrealizedMTM"];
  const COL_LABEL: Record<string, string> = {
    OurLTP: "Last price (ours)",
    OurMTM: "MTM (ours, gross)",
  };
  const MONEY_COLS = new Set(["OurMTM", "RealizedMTM", "MTM", "UnrealizedMTM"]);
  const cell = (c: string, v: unknown) =>
    MONEY_COLS.has(c) ? inr2(v as number | string | null) : c === "ExchangeInstrumentId" || c === "AppOrderID" ? String(v ?? "—") : num2(v);
  const ordCols = ["AppOrderID", "OrderSide", "OrderQuantity", "OrderType", "OrderStatus",
    "OrderAverageTradedPrice", "CancelRejectReason"];

  const presentCols = (rows: Record<string, unknown>[], candidates: string[]) =>
    candidates.filter((c) => rows.some((r) => r[c] !== undefined && r[c] !== ""));

  return (
    <Card
      title="Live Account — Lakshmishree"
      hint="read-only broker state: balance, positions, today's orders"
    >
      <div className="flex items-center gap-2 mb-2">
        <button type="button" className="pill text-xs" disabled={busy} onClick={() => void load()}>
          {busy ? "Refreshing…" : "Refresh"}
        </button>
        {acct && (
          <span className={`text-[11px] font-bold ${acct.connected ? "text-pe" : "text-ce"}`}>
            {acct.connected ? "● session live" : `✗ ${acct.error ?? "not connected"}`}
          </span>
        )}
        {err && <span className="text-[11px] text-ce">{err}</span>}
        {acct?.connected && (acct as { fetched_ms?: number }).fetched_ms != null && (
          <span className="text-[10px] text-muted">fetched in {(acct as { fetched_ms?: number }).fetched_ms} ms</span>
        )}
      </div>

      {snap && eff && (
        <div className="text-[11px] text-amber-300 bg-amber-950/30 border border-amber-700/40 rounded-lg px-3 py-1.5 mb-2">
          ⏳ Broker gateway offline — showing the LAST snapshot, fetched{" "}
          {snap.fetched_at.slice(0, 16).replace("T", " ")} UTC. It refreshes
          automatically the next time the gateway answers (market hours).
        </div>
      )}

      {eff && (
        <>
          {highlights.length > 0 && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
              {highlights.map(([label, val]) => (
                <div key={label} className="bg-panel/60 border border-border rounded-lg px-3 py-2">
                  <div className="text-[9.5px] text-muted uppercase">{label}</div>
                  <div className="text-sm font-bold text-gray-100">{inr2(val)}</div>
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] text-muted uppercase mb-1">Net positions ({eff.positions.length})</div>
          {eff.positions.length === 0 ? (
            <div className="text-xs text-muted mb-3">No open positions.</div>
          ) : (
            <div className="overflow-x-auto mb-3">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-muted text-left">
                    {presentCols(eff.positions, posCols).map((c) => (
                      <th key={c} className="font-medium pr-3 pb-1">{COL_LABEL[c] ?? c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {eff.positions.map((r, i) => (
                    <tr key={i} className="border-t border-border/40">
                      {presentCols(eff.positions, posCols).map((c) => (
                        <td
                          key={c}
                          className={`pr-3 py-1 font-mono ${c === "OurMTM" ? `font-bold ${pnlTone(Number(r[c]))}` : ""}`}
                          title={c === "OurLTP" && r.OurLTPAt ? `newest tick ${String(r.OurLTPAt)}` : undefined}
                        >
                          {cell(c, r[c])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="text-[10px] text-muted uppercase mb-1">Orders ({eff.orders.length})</div>
          {eff.orders.length === 0 ? (
            <div className="text-xs text-muted mb-3">No orders today.</div>
          ) : (
            <div className="overflow-x-auto mb-3">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-muted text-left">
                    {presentCols(eff.orders, ordCols).map((c) => (
                      <th key={c} className="font-medium pr-3 pb-1">{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {eff.orders.map((r, i) => (
                    <tr key={i} className="border-t border-border/40">
                      {presentCols(eff.orders, ordCols).map((c) => (
                        <td key={c} className="pr-3 py-1 font-mono">{cell(c, r[c])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {balanceRows.length > 0 && (
            <details className="text-[11px]">
              <summary className="cursor-pointer text-muted hover:text-gray-200">
                Full balance detail ({balanceRows.length} fields — everything the broker reports)
              </summary>
              <div className="mt-2 max-h-64 overflow-y-auto border border-border/40 rounded-lg">
                <table className="w-full">
                  <tbody>
                    {balanceRows.map(([k, v]) => (
                      <tr key={k} className="border-t border-border/30 first:border-t-0">
                        <td className="px-2 py-0.5 text-muted font-mono text-[10px]">{k}</td>
                        <td className="px-2 py-0.5 font-mono">{num2(v)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}

      <div className="mt-2">
        <CalcNote>
          This card is the BROKER account's state. The algo's own live trading P&L
          (its entries/exits) lives in P&L Summary with the ledger switched to LIVE.
          The broker's margin figures can lag a fill by a few minutes and its MTM
          fields often read 0.00 — "Last price (ours)" and "MTM (ours)" come from this
          platform's own newest tick, and the positions/orders tables update at once.
        </CalcNote>
      </div>
    </Card>
  );
}

function ConnRow(props: { name: string; meta: string; ok: boolean; badge?: string; tone?: "ok" | "bad" | "off" }) {
  const tone = props.tone ?? (props.ok ? "ok" : "off");
  return (
    <div className="flex items-center justify-between py-2 border-b border-border/40 last:border-b-0">
      <div>
        <div className="text-[12.5px] font-semibold text-gray-100">{props.name}</div>
        <div className="text-[10.5px] text-muted">{props.meta}</div>
      </div>
      <span
        className={`text-[11px] font-bold px-2.5 py-0.5 rounded-full border ${
          tone === "ok"
            ? "text-pe border-pe/40 bg-pe/10"
            : tone === "bad"
              ? "text-ce border-ce/40 bg-ce/10"
              : "text-muted border-border bg-panel"
        }`}
      >
        {props.badge ?? (props.ok ? "Connected" : "Not connected")}
      </span>
    </div>
  );
}

/** §5 Telegram: status-driven row (getMe probe, cached server-side) plus a
 *  "Send test message" button. The token never reaches the browser — only the
 *  bot username and a masked chat id. */
function TelegramRow() {
  const [st, setSt] = useState<TelegramStatus | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "probe" | "test">("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async (force = false) => {
    try {
      setSt(await algoApi.telegramStatus(force));
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const meta = loadErr
    ? `status unavailable — ${loadErr}`
    : !st
      ? "checking…"
      : !st.configured
        ? "not configured — set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the backend .env"
        : st.ok
          ? `@${st.bot_username ?? "bot"} → chat ${st.chat_id_masked ?? "?"}${
              st.checked_at ? ` · checked ${st.checked_at.slice(11, 19)}` : ""
            }`
          : st.ok === false
            ? `configured, probe failed: ${st.error ?? "unknown error"}`
            : "configured — probe pending";
  const tone: "ok" | "bad" | "off" = !st || loadErr
    ? "off"
    : !st.configured
      ? "off"
      : st.ok
        ? "ok"
        : st.ok === false
          ? "bad"
          : "off";
  const badge = !st || loadErr
    ? "…"
    : !st.configured
      ? "Not configured"
      : st.ok
        ? "Connected"
        : st.ok === false
          ? "Not connected"
          : "Unknown";

  return (
    <>
      <ConnRow name="Telegram Bot — Alerts" meta={meta} ok={tone === "ok"} tone={tone} badge={badge} />
      <div className="flex items-center gap-2 py-1.5 flex-wrap">
        <button
          type="button"
          className="pill text-xs"
          disabled={busy !== "" || !st?.configured}
          title={
            st?.configured
              ? "Sends a one-off test line to the configured chat (bypasses alert throttling)"
              : "Configure the bot token and chat id in the backend .env first"
          }
          onClick={() =>
            void (async () => {
              setBusy("test");
              setMsg("sending…");
              try {
                const r = await algoApi.telegramTest();
                setMsg(`✔ ${r.detail || "sent"}`);
                void load();
              } catch (e) {
                const detail = e instanceof Error ? e.message : String(e);
                setMsg(
                  e instanceof AlgoApiError && e.status === 409
                    ? `✗ ${detail}`
                    : e instanceof AlgoApiError && e.status === 502
                      ? `✗ Telegram refused: ${detail}`
                      : `✗ ${detail}`
                );
              } finally {
                setBusy("");
              }
            })()
          }
        >
          {busy === "test" ? "Sending…" : "Send test message"}
        </button>
        <button
          type="button"
          className="pill text-xs"
          disabled={busy !== ""}
          title={`Re-run the getMe probe now${st ? ` (server caches for ${st.min_interval_s}s)` : ""}`}
          onClick={() =>
            void (async () => {
              setBusy("probe");
              try {
                await load(true);
              } finally {
                setBusy("");
              }
            })()
          }
        >
          {busy === "probe" ? "Checking…" : "Re-check"}
        </button>
        {msg && <span className="text-[11px] text-muted">{msg}</span>}
      </div>
    </>
  );
}

// ── Paper Trading settings ────────────────────────────────────────────────

export function PaperPanel({
  draft, mutate, showLiveSession = true, onAfterReset,
}: PanelProps & { showLiveSession?: boolean; onAfterReset?: () => void }) {
  const p = draft.global.paper;
  const [session, setSession] = useState<PaperSession | null>(null);
  const [resetBusy, setResetBusy] = useState(false);

  const loadSession = useCallback(async () => {
    try {
      setSession(await algoApi.paperSession());
    } catch {
      setSession(null);
    }
  }, []);

  // Poll every 5 s while the simulator holds a position so the Unrealized /
  // Equity tiles track the live LTP; a flat session only loads once.
  const positionOpen = session?.open_position != null;
  useEffect(() => {
    if (!showLiveSession) return;
    void loadSession();
    if (!positionOpen) return;
    const id = window.setInterval(() => void loadSession(), 5000);
    return () => window.clearInterval(id);
  }, [loadSession, showLiveSession, positionOpen]);

  const doReset = useCallback(async () => {
    if (
      !window.confirm(
        "Reset the paper session? This clears ALL paper trade history and restarts the session today. The live ledger is untouched."
      )
    ) {
      return;
    }
    setResetBusy(true);
    try {
      await algoApi.paperReset();
      await loadSession();
      // The reset is a server-side config save (session_started → today):
      // reload the page's saved document so the next save cannot revert it
      // (QA 2026-09-02 H1).
      onAfterReset?.();
    } finally {
      setResetBusy(false);
    }
  }, [loadSession, onAfterReset]);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Card title="Virtual Capital">
        {/* Same draft field as the Daily Trading Config header switch — one
            source of truth (global.paper.paper_mode), staged until Save. */}
        <div className="mb-3">
          <ToggleLine
            name={p.paper_mode ? "Paper mode ON" : "Paper mode OFF — LIVE"}
            desc={
              p.paper_mode
                ? "Every order goes to the simulator. Switch OFF to route orders to the LIVE broker."
                : "Orders go to the LIVE broker with real money. Switch ON to simulate instead."
            }
            on={p.paper_mode}
            onChange={(v) => mutate((d) => void (d.global.paper.paper_mode = v))}
          />
          {!p.paper_mode && (
            <div className="mt-1 text-center font-extrabold text-[11.5px] tracking-wide text-black bg-red-400 rounded-md py-1.5">
              ⚠ LIVE MODE — orders will hit the broker once saved
            </div>
          )}
        </div>
        <NumField
          label="Virtual Balance (₹) — independent of the real Demat balance"
          value={p.virtual_balance}
          onChange={(v) => mutate((d) => void (d.global.paper.virtual_balance = v))}
        />
        {showLiveSession && session && (
          <div className="grid grid-cols-2 gap-2 mt-3">
            <div className="bg-panel/60 border border-border rounded-lg px-3 py-2">
              <div className="text-[9.5px] text-muted uppercase">Session realized P&L</div>
              <div
                className={`text-base font-bold ${
                  session.realized_pnl > 0 ? "text-pe" : session.realized_pnl < 0 ? "text-ce" : "text-gray-100"
                }`}
              >
                {session.realized_pnl >= 0 ? "+" : ""}₹{Math.round(session.realized_pnl).toLocaleString("en-IN")}
              </div>
            </div>
            <div className="bg-panel/60 border border-border rounded-lg px-3 py-2">
              <div className="text-[9.5px] text-muted uppercase">Current virtual balance</div>
              <div className="text-base font-bold text-accent">
                ₹{Math.round(session.current_balance).toLocaleString("en-IN")}
              </div>
            </div>
          </div>
        )}
        {showLiveSession && session && <PaperPositionTiles session={session} />}
        {showLiveSession ? (
          <>
            <div className="text-[10.5px] text-muted mt-2">
              Session started:{" "}
              <span className="font-mono">{session?.session_started ?? p.session_started ?? "—"}</span>
            </div>
            <button
              type="button"
              className="pill text-xs mt-3 text-ce border border-ce/30 disabled:opacity-50"
              disabled={resetBusy}
              onClick={() => void doReset()}
            >
              {resetBusy ? "Resetting…" : "Reset Paper Session"}
            </button>
          </>
        ) : (
          <CalcNote>
            SANDBOX: this virtual balance is the default starting capital for
            backtest runs (the New-Run form can override it per run). Slippage
            and fees below drive every backtest fill.
          </CalcNote>
        )}
        <div className="mt-3">
          <ToggleLine
            name="Shadow Mode"
            desc="Mirror every live signal into a parallel paper trade with identical configuration"
            on={p.shadow_mode}
            onChange={(v) => mutate((d) => void (d.global.paper.shadow_mode = v))}
          />
        </div>
      </Card>
      <Card title="Simulated Order Execution">
        <div className="grid grid-cols-2 gap-3">
          <NumField
            label="Slippage (%) — applied unfavourably"
            value={p.slippage_pct}
            step={0.1}
            onChange={(v) => mutate((d) => void (d.global.paper.slippage_pct = v))}
          />
          <NumField
            label="Simulated latency (ms, 0–2000) — live paper only"
            value={p.latency_ms}
            onChange={(v) =>
              mutate(
                (d) =>
                  void (d.global.paper.latency_ms = Math.min(
                    2000,
                    Math.max(0, Math.round(Number.isFinite(v) ? v : 0))
                  ))
              )
            }
          />
        </div>
        <div className="mt-3 w-56">
          <span className="block text-[10px] text-muted mb-1">Fill source</span>
          <div
            className="bg-panel/60 border border-border rounded-lg px-2 py-1.5 text-sm text-gray-100"
            title="The feed stores LTP only — bid/ask mid cannot be simulated, so the fill source is fixed."
          >
            Live LTP (OI Algo feed)
          </div>
          {p.fill_source !== "ltp" && (
            <div className="text-[10.5px] text-amber-300 mt-1">
              Saved value "{p.fill_source}" is not supported — the simulator treats it as Live LTP.
            </div>
          )}
        </div>
        <div className="mt-3">
          <CalcNote>
            <b>Latency:</b> in LIVE paper trading the simulator really waits this long (at most
            2 s) before committing the fill; the fill price is the LTP the decision was taken on
            — there is no re-pricing after the wait — and slippage is then applied
            unfavourably. Backtests keep deterministic minute-close fills; latency is recorded
            on the fill, not simulated there. <b>Fill source:</b> the feed stores LTP only, so
            Bid/Ask mid cannot be simulated. Fees per trade come from the Integrations tab's fee
            model — identical for paper and live. The full paper P&L lives in the P&L Summary
            tab (ledger = PAPER).
          </CalcNote>
        </div>
        <div className="mt-2">
          <CalcNote>
            Paper mode runs the identical decision path — same indicators, gates, risk limits
            and exits as live; only execution routes to the simulator instead of the broker.
          </CalcNote>
        </div>
      </Card>
      <div className="md:col-span-2">
        <BrokerageCalculator fees={draft.global.fees} />
      </div>
    </div>
  );
}

/** §8 paper completeness: the simulator's open position, its mark-to-market
 *  and the resulting equity. Rendered from `/paper/session`, polled every 5 s
 *  by the parent while a position is open. */
function PaperPositionTiles({ session }: { session: PaperSession }) {
  const pos = session.open_position ?? null;
  // The paper session only tracks PAPER positions. A live trade used to leave
  // this tile reading "flat" while real money was open.
  const streamPos = streamPosition(useAlgoStreamContext()?.frame);
  const liveElsewhere = !pos && streamPos && streamPos.ledger === "live" ? streamPos : null;
  const unreal = session.unrealized_pnl ?? pos?.unrealized_gross ?? 0;
  const equity = session.equity ?? session.current_balance + unreal;
  const pnlCls = (n: number) => (n > 0 ? "text-pe" : n < 0 ? "text-ce" : "text-gray-100");
  const inr = (n: number, signed = false) =>
    `${signed && n > 0 ? "+" : ""}${n < 0 ? "−" : ""}₹${Math.round(Math.abs(n)).toLocaleString("en-IN")}`;
  return (
    <div className="grid grid-cols-3 gap-2 mt-2">
      <div
        className="bg-panel/60 border border-border rounded-lg px-3 py-2"
        title={
          pos
            ? `trade #${pos.trade_id} · entered ${pos.entry_ts.slice(11, 16)} @ ₹${pos.entry_fill.toFixed(2)}${
                pos.last_close != null ? ` · last ₹${pos.last_close.toFixed(2)}` : ""
              }`
            : "No open paper position"
        }
      >
        <div className="text-[9.5px] text-muted uppercase">Open position</div>
        {pos ? (
          <>
            <div className={`text-sm font-bold ${pos.side === "CALL" ? "text-pe" : "text-ce"}`}>
              {pos.strike ?? "—"}
              {pos.option_type ?? ""} × {pos.lots} lot{pos.lots === 1 ? "" : "s"}
            </div>
            <div className="text-[10px] text-muted font-mono">
              in @ {pos.entry_fill.toFixed(2)}
              {pos.last_close != null ? ` · now ${pos.last_close.toFixed(2)}` : ""}
            </div>
          </>
        ) : liveElsewhere ? (
          <div data-testid="paper-tile-live-open">
            <div className="text-sm font-bold text-ce">LIVE trade #{liveElsewhere.trade_id} open</div>
            <div className="text-[10px] text-muted font-mono">
              {liveElsewhere.side} {positionLabel(liveElsewhere)} × {liveElsewhere.lots} · not part of this paper session
            </div>
          </div>
        ) : (
          <div className="text-sm font-bold text-muted">flat (paper)</div>
        )}
      </div>
      <div
        className="bg-panel/60 border border-border rounded-lg px-3 py-2"
        title="Mark-to-market of the open position at the latest LTP (gross, before exit fees)"
      >
        <div className="text-[9.5px] text-muted uppercase">Unrealized</div>
        <div className={`text-base font-bold ${pos ? pnlCls(unreal) : "text-muted"}`}>
          {pos ? inr(unreal, true) : "—"}
        </div>
        {pos && pos.unrealized_pct != null && (
          <div className={`text-[10px] ${pnlCls(pos.unrealized_pct)}`}>
            {pos.unrealized_pct > 0 ? "+" : ""}
            {pos.unrealized_pct.toFixed(2)}%
          </div>
        )}
      </div>
      <div
        className="bg-panel/60 border border-border rounded-lg px-3 py-2"
        title={`Current virtual balance + unrealized · today: ${session.trades_today ?? 0} trade(s), realized ${inr(session.realized_today ?? 0, true)}`}
      >
        <div className="text-[9.5px] text-muted uppercase">Equity</div>
        <div className="text-base font-bold text-accent">{inr(equity)}</div>
        <div className="text-[10px] text-muted">
          today {session.trades_today ?? 0} · {inr(session.realized_today ?? 0, true)}
        </div>
      </div>
    </div>
  );
}

// ── Brokerage calculator (what-if round-trip cost, same math as fees.py) ──

function BrokerageCalculator({ fees }: { fees: AlgoConfigDoc["global"]["fees"] }) {
  const [buy, setBuy] = useState(100);
  const [sell, setSell] = useState(110);
  const [lots, setLots] = useState(1);
  const [lotSize, setLotSize] = useState(65);

  // EXACT mirror of backend round_trip_fees (app/algo/fees.py) — the same
  // editable rates from the Integrations tab feed both.
  const qty = lotSize * lots;
  const buyTurn = buy * qty;
  const sellTurn = sell * qty;
  const both = buyTurn + sellTurn;
  const r = (x: number) => Math.round(x * 100) / 100;
  const brokerage = r(fees.brokerage_per_order * 2);
  const stt = r((sellTurn * fees.stt_sell_premium_pct) / 100);
  const txn = r((both * fees.exchange_txn_pct) / 100);
  const sebi = r((both * fees.sebi_turnover_pct) / 100);
  const ipft = r((both * fees.ipft_pct) / 100);
  const gst = r(((fees.brokerage_per_order * 2 + (both * fees.exchange_txn_pct) / 100 + (both * fees.sebi_turnover_pct) / 100) * fees.gst_pct) / 100);
  const stamp = r((buyTurn * fees.stamp_duty_buy_pct) / 100);
  const total = r(brokerage + stt + txn + sebi + ipft + gst + stamp);
  const gross = r((sell - buy) * qty);
  const net = r(gross - total);
  const breakeven = qty > 0 ? r(total / qty) : 0;

  const Row = ({ k, v }: { k: string; v: number }) => (
    <div className="flex items-center justify-between py-0.5 text-[11.5px]">
      <span className="text-muted">{k}</span>
      <span className="font-mono text-gray-100">₹{v.toFixed(2)}</span>
    </div>
  );

  return (
    <Card
      title="Brokerage Calculator"
      hint="what a round trip really costs — identical math to the simulator and live P&L"
    >
      <div className="grid md:grid-cols-2 gap-4">
        <div className="grid grid-cols-2 gap-3">
          <NumField label="Buy premium (₹)" value={buy} step={0.05} onChange={setBuy} />
          <NumField label="Sell premium (₹)" value={sell} step={0.05} onChange={setSell} />
          <NumField label="Lots" value={lots} onChange={(v) => setLots(Math.max(1, Math.round(v)))} />
          <NumField label="Lot size" value={lotSize} onChange={(v) => setLotSize(Math.max(1, Math.round(v)))} />
          <div className="col-span-2">
            <CalcNote>
              Rates come live from the Brokerage &amp; Statutory Charges card (Integrations tab)
              — edit them there and this calculator follows.
            </CalcNote>
          </div>
        </div>
        <div className="bg-panel/60 border border-border rounded-lg px-3.5 py-2.5">
          <Row k={`Brokerage (2 orders × ₹${fees.brokerage_per_order})`} v={brokerage} />
          <Row k={`STT ${fees.stt_sell_premium_pct}% (sell side)`} v={stt} />
          <Row k={`Exchange txn ${fees.exchange_txn_pct}%`} v={txn} />
          <Row k={`SEBI ${fees.sebi_turnover_pct}%`} v={sebi} />
          <Row k={`IPFT ${fees.ipft_pct}%`} v={ipft} />
          <Row k={`GST ${fees.gst_pct}% (on brokerage+txn+SEBI)`} v={gst} />
          <Row k={`Stamp duty ${fees.stamp_duty_buy_pct}% (buy side)`} v={stamp} />
          <div className="border-t border-border/60 mt-1.5 pt-1.5">
            <div className="flex items-center justify-between text-[12.5px] font-bold">
              <span className="text-gray-100">Total charges</span>
              <span className="font-mono text-amber-300">₹{total.toFixed(2)}</span>
            </div>
            <div className="flex items-center justify-between text-[11.5px] mt-0.5">
              <span className="text-muted">Gross P&L ({qty} units)</span>
              <span className={`font-mono ${gross >= 0 ? "text-pe" : "text-ce"}`}>
                {gross >= 0 ? "+" : ""}₹{gross.toFixed(2)}
              </span>
            </div>
            <div className="flex items-center justify-between text-[12.5px] font-bold mt-0.5">
              <span className="text-gray-100">Net P&L after charges</span>
              <span className={`font-mono ${net >= 0 ? "text-pe" : "text-ce"}`}>
                {net >= 0 ? "+" : ""}₹{net.toFixed(2)}
              </span>
            </div>
            <div className="text-[10px] text-muted mt-1">
              Breakeven: the premium must move ₹{breakeven.toFixed(2)} in your favour just to
              cover charges.
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
}

