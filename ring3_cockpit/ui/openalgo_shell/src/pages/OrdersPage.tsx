/**
 * SmartOMS Orders & Positions (sec_009, ADR-009).
 *
 * The blotter is split into three books: working Maker-Peg orders and TWAP /
 * Iceberg slicers, per-silo positions reconciled against the ledger, and the
 * two-phase outbox. Order entry and the cancel sweep post through the paper
 * endpoints only; the live surface keeps the synthetic ticket off the book.
 */

import { useState, type ReactNode } from "react";
import { Ban, Send } from "lucide-react";
import { api, postCancelOrders, postPaperOrder } from "@/lib/api";
import { useAction, useResource } from "@/lib/use-api";
import { useLocation } from "@/lib/router";
import { ago, int, isFiniteNumber, microLatency, num, record, shortId, text } from "@/lib/format";
import type { CancelOrdersReceipt, JsonRecord, PaperOrderReceipt } from "@/lib/types";
import {
  ConfirmDangerModal,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabStrip,
  TabularValue,
  type PillTone,
} from "@/design-system/primitives";
import { pushToast, toastError } from "@/lib/toast";

const FALLBACK_SILOS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"];

const POSITION_FIELDS = ["qty_micros", "quantity", "qty", "price_micros", "avg_price", "notional"];

type OrderTab = "orders" | "positions" | "outbox";

interface PositionRow {
  key: string;
  silo: string;
  symbol: string;
  quantity: number;
  avgPrice: number;
  notional: number;
  raw: JsonRecord;
}

interface ReconState {
  available: boolean;
  frozen: Set<string>;
}

function normalizeKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function numberField(body: JsonRecord, names: string[]): number | null {
  for (const name of names) {
    const value = body[name];
    if (isFiniteNumber(value)) return value;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

function stateTone(value: string): PillTone {
  const label = value.toUpperCase();
  if (["WORKING", "FILLED", "COMMITTED", "ARMED", "RECONCILED", "SENT"].includes(label)) return "ok";
  if (["PENDING", "REPEGGED", "PARTIALLY_FILLED", "UNTRACKED"].includes(label)) return "warn";
  if (["CANCELED", "CANCELLED", "REJECTED", "VOIDED", "HALTED", "EXPIRED", "FAILED"].includes(label)) return "bad";
  return "neutral";
}

function positionFrom(silo: string, symbol: string, body: JsonRecord, key: string): PositionRow {
  const qtyMicros = numberField(body, ["qty_micros"]);
  const priceMicros = numberField(body, ["price_micros"]);
  const notionalMicros = numberField(body, ["notional_micros"]);
  const quantity =
    numberField(body, ["quantity", "qty", "net_qty"]) ?? (qtyMicros === null ? 0 : qtyMicros / 1_000_000);
  const avgPrice =
    numberField(body, ["avg_price", "average_price", "price"]) ?? (priceMicros === null ? 0 : priceMicros / 1_000_000);
  const notional =
    numberField(body, ["notional", "notional_value"]) ??
    (notionalMicros === null ? quantity * avgPrice : notionalMicros / 1_000_000);
  return {
    key,
    silo: text(body.silo, silo),
    symbol: text(body.symbol, symbol),
    quantity,
    avgPrice,
    notional,
    raw: body,
  };
}

function positionsFromArray(items: unknown[], fallbackSilo: string): PositionRow[] {
  const out: PositionRow[] = [];
  items.forEach((item, index) => {
    const body = record(item);
    if (Object.keys(body).length === 0) return;
    const symbol = text(body.symbol ?? body.instrument, "");
    const silo = text(body.silo ?? body.owner, fallbackSilo);
    out.push(positionFrom(silo, symbol, body, `${silo}:${symbol}:${index}`));
  });
  return out;
}

function positionRows(positions: unknown): PositionRow[] {
  if (Array.isArray(positions)) return positionsFromArray(positions, "");
  const body = record(positions);
  const nested = body.silos;
  if (Array.isArray(nested)) return positionsFromArray(nested, "");
  const table = nested !== null && typeof nested === "object" ? record(nested) : body;
  const out: PositionRow[] = [];
  for (const [key, value] of Object.entries(table)) {
    if (Array.isArray(value)) {
      out.push(...positionsFromArray(value, key));
      continue;
    }
    const inner = record(value);
    if (Object.keys(inner).length === 0) continue;
    const leaf = POSITION_FIELDS.some((field) => field in inner);
    if (leaf) {
      const parts = key.split(":");
      const silo = parts.length > 1 ? parts[0] : key;
      const symbol = parts.length > 1 ? parts.slice(1).join(":") : text(inner.symbol, "");
      out.push(positionFrom(silo, symbol, inner, key));
      continue;
    }
    for (const [symbolKey, leafValue] of Object.entries(inner)) {
      const leafBody = record(leafValue);
      if (Object.keys(leafBody).length === 0) continue;
      out.push(positionFrom(key, symbolKey, leafBody, `${key}:${symbolKey}`));
    }
  }
  return out;
}

function reconciliationState(reconciliation: JsonRecord | undefined): ReconState {
  const snapshot = record(reconciliation?.snapshot);
  const rawFrozen = snapshot.frozen;
  const frozenList: unknown[] = Array.isArray(rawFrozen) ? rawFrozen : [];
  return {
    available: reconciliation?.available === true,
    frozen: new Set(frozenList.map((value) => normalizeKey(text(value, "")))),
  };
}

function OrderBookTable({ rows }: { rows: JsonRecord[] }): ReactNode {
  if (rows.length === 0) {
    return (
      <EmptyCleanSlate
        title="No working orders"
        body="No Maker-Peg chaser is resting. The next paper order appears here within one reconciliation window."
      />
    );
  }
  return (
    <div className="max-h-[520px] overflow-auto">
      <table className="table table-compact zebra">
        <caption className="sr-only">Working Maker-Peg orders with their re-peg budget and execution state</caption>
        <thead>
          <tr>
            <th scope="col">Order</th>
            <th scope="col">Silo</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col" className="num">
              Qty
            </th>
            <th scope="col" className="num">
              Filled
            </th>
            <th scope="col" className="num">
              Price
            </th>
            <th scope="col">Style</th>
            <th scope="col" className="num">
              Re-pegs
            </th>
            <th scope="col">Status</th>
            <th scope="col">Created</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const id = shortId(row.order_id ?? row.slicer_id ?? row.id);
            const status = text(row.state ?? row.status, "WORKING");
            const side = text(row.side, "—");
            const repegs = numberField(row, ["repegs"]);
            const remaining = numberField(row, ["repegs_remaining"]);
            return (
              <tr key={`${id}-${index}`}>
                <td className="mono">{id}</td>
                <td>{text(row.silo, "—")}</td>
                <td className="mono">{text(row.symbol, "—")}</td>
                <td className={side.toUpperCase() === "BUY" ? "pos" : "neg"}>{side}</td>
                <td className="num">
                  <TabularValue value={numberField(row, ["qty", "quantity", "total_quantity"])} kind="num" />
                </td>
                <td className="num">
                  <TabularValue value={numberField(row, ["filled_quantity", "filled_qty", "filled"])} kind="num" />
                </td>
                <td className="num">
                  <TabularValue value={numberField(row, ["price", "limit_price", "avg_price"])} kind="price" />
                </td>
                <td className="mono text-[10px] text-[var(--muted)]">
                  {text(row.kind ?? row.order_style ?? row.algorithm, "MAKER_PEG")}
                </td>
                <td className="num">
                  {repegs === null ? (
                    "—"
                  ) : (
                    <span className="mono tnum">
                      {num(repegs, 0)}
                      {remaining === null ? "" : ` / ${num(repegs + remaining, 0)}`}
                    </span>
                  )}
                </td>
                <td>
                  <StatusPill value={status} tone={stateTone(status)} />
                </td>
                <td className="mono text-[10px] whitespace-nowrap text-[var(--dim)]">
                  {ago(row.created_ns ?? row.created_at_ns)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SlicerTable({ rows }: { rows: JsonRecord[] }): ReactNode {
  if (rows.length === 0) {
    return (
      <p className="mono px-1 py-2 text-[10.5px] text-[var(--dim)]">
        No TWAP or Iceberg slicer is running on this workspace.
      </p>
    );
  }
  return (
    <div className="max-h-[280px] overflow-auto">
      <table className="table table-compact zebra">
        <caption className="sr-only">TWAP and Iceberg slicers with their slice progress</caption>
        <thead>
          <tr>
            <th scope="col">Slicer</th>
            <th scope="col">Silo</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col" className="num">
              Qty
            </th>
            <th scope="col" className="num">
              Filled
            </th>
            <th scope="col" className="num">
              Slices
            </th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const status = text(row.state ?? row.status, "WORKING");
            return (
              <tr key={`${text(row.slicer_id ?? row.order_id, "slicer")}-${index}`}>
                <td className="mono">{shortId(row.slicer_id ?? row.order_id)}</td>
                <td>{text(row.silo, "—")}</td>
                <td className="mono">{text(row.symbol, "—")}</td>
                <td>{text(row.side, "—")}</td>
                <td className="num">
                  <TabularValue value={numberField(row, ["total_quantity", "quantity", "qty"])} kind="num" />
                </td>
                <td className="num">
                  <TabularValue value={numberField(row, ["filled_quantity", "filled_qty", "filled"])} kind="num" />
                </td>
                <td className="num">
                  <TabularValue value={numberField(row, ["slice_count", "slices", "total_slices"])} kind="int" />
                </td>
                <td>
                  <StatusPill value={status} tone={stateTone(status)} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PositionTable({ rows, recon }: { rows: PositionRow[]; recon: ReconState }): ReactNode {
  if (rows.length === 0) {
    return (
      <EmptyCleanSlate
        title="No positions"
        body="The IBOR holds no open lot. A filled paper order writes the first position here."
      />
    );
  }
  return (
    <div className="max-h-[520px] overflow-auto">
      <table className="table table-compact zebra">
        <caption className="sr-only">Open positions with the ledger mark and the reconciliation verdict</caption>
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Silo</th>
            <th scope="col" className="num">
              Quantity
            </th>
            <th scope="col" className="num">
              Avg price
            </th>
            <th scope="col" className="num">
              Mark
            </th>
            <th scope="col" className="num">
              Unrealised
            </th>
            <th scope="col" className="num">
              Notional
            </th>
            <th scope="col">Ledger state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const mark = numberField(row.raw, ["mark", "mark_price", "last", "ltp"]);
            const unrealised = numberField(row.raw, ["unrealised", "unrealized", "unrealised_pnl", "pnl"]);
            const frozen = recon.frozen.has(normalizeKey(row.silo));
            const label = frozen ? "FROZEN" : recon.available ? "RECONCILED" : "UNAVAILABLE";
            const tone: PillTone = frozen ? "bad" : recon.available ? "ok" : "neutral";
            return (
              <tr key={row.key}>
                <td className="mono">{row.symbol || "—"}</td>
                <td>{row.silo || "—"}</td>
                <td className="num">
                  <TabularValue value={row.quantity} kind="num" digits={4} />
                </td>
                <td className="num">
                  <TabularValue value={row.avgPrice} kind="price" />
                </td>
                <td className="num">
                  <TabularValue value={mark} kind="price" />
                </td>
                <td className="num">
                  <TabularValue value={unrealised} kind="usd" signTone />
                </td>
                <td className="num">
                  <TabularValue value={row.notional} kind="usd" />
                </td>
                <td>
                  <StatusPill value={label} tone={tone} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ReservationTable({ rows }: { rows: JsonRecord[] }): ReactNode {
  if (rows.length === 0) {
    return (
      <EmptyCleanSlate
        title="Outbox empty"
        body="No two-phase reservation is journalled. Every paper fill opens one before it touches the ledger."
      />
    );
  }
  return (
    <div className="max-h-[520px] overflow-auto">
      <table className="table table-compact zebra">
        <caption className="sr-only">Two-phase outbox reservations with their transfer id and state</caption>
        <thead>
          <tr>
            <th scope="col">Transfer</th>
            <th scope="col">Silo</th>
            <th scope="col">Vault</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col" className="num">
              Amount
            </th>
            <th scope="col">State</th>
            <th scope="col">Created</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const transfer = numberField(row, ["pending_transfer_id", "transfer_id"]);
            const state = text(row.state, "PENDING").toUpperCase();
            return (
              <tr key={`${transfer === null ? "res" : num(transfer, 0)}-${index}`}>
                <td className="mono tnum">{transfer === null ? "—" : num(transfer, 0)}</td>
                <td>{text(row.silo, "—")}</td>
                <td>{text(row.vault, "—")}</td>
                <td className="mono">{text(row.symbol, "—")}</td>
                <td>{text(row.side, "—")}</td>
                <td className="num">
                  <TabularValue value={numberField(row, ["total_lock_micros", "notional_micros"])} kind="money" />
                </td>
                <td>
                  <StatusPill value={state} tone={stateTone(state)} />
                </td>
                <td className="mono text-[10px] whitespace-nowrap text-[var(--dim)]">
                  {ago(row.created_ns ?? row.created_at_ns)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function OrdersPage(): ReactNode {
  const location = useLocation();
  const isLive = location.workspace === "live";

  const { data, error, loading, reload } = useResource("orders-page", () => api.orders(), 6000);
  const order = useAction<PaperOrderReceipt>();
  const cancel = useAction<CancelOrdersReceipt>();

  const [tab, setTab] = useState<OrderTab>("orders");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [silo, setSilo] = useState("");
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("1");
  const [style, setStyle] = useState<"MAKER_PEG" | "TWAP_ICEBERG">("MAKER_PEG");

  const working = data?.working_orders ?? [];
  const twaps = data?.twap_icebergs ?? [];
  const events = data?.events ?? [];
  const reservations = data?.reservations ?? [];
  const positions = positionRows(data?.positions);
  const recon = reconciliationState(data?.reconciliation);
  const pendingCount = data?.pending_reservations ?? 0;
  const repegs = events.filter((event) =>
    `${text(event.event_type, "")} ${text(event.type, "")}`.toUpperCase().includes("PEG"),
  ).length;

  const siloOptions = (() => {
    const nested = record(record(data?.positions).silos);
    const keys = Object.keys(nested);
    return keys.length > 0 ? keys : FALLBACK_SILOS;
  })();
  const activeSilo = silo || siloOptions[0] || FALLBACK_SILOS[0];

  const outbox = record(data?.outbox);
  const outboxCounts = record(outbox.counts);
  const outboxPending = numberField(outboxCounts, ["PENDING"]) ?? numberField(outbox, ["pending"]) ?? pendingCount;
  const outboxCommitted = numberField(outboxCounts, ["COMMITTED"]) ?? 0;
  const outboxVoided = numberField(outboxCounts, ["VOIDED"]) ?? 0;
  const reservationCount = numberField(outbox, ["reservations"]) ?? reservations.length;
  const lastLatencyNs = numberField(outbox, ["last_latency_ns"]);
  const budgetNs = numberField(outbox, ["latency_budget_ns"]);

  const submitOrder = async (): Promise<void> => {
    const quantity = Number(qty);
    const cleanSymbol = symbol.trim().toUpperCase();
    if (!activeSilo) {
      pushToast("Select a silo", "The order must be routed into one silo", "warn");
      return;
    }
    if (cleanSymbol === "") {
      pushToast("Enter a symbol", "The OMS needs an instrument to route", "warn");
      return;
    }
    if (!Number.isFinite(quantity) || quantity <= 0) {
      pushToast("Enter a positive quantity", "Fractional paper quantities are allowed", "warn");
      return;
    }
    const receipt = await order.run(() =>
      postPaperOrder({
        silo: activeSilo,
        symbol: cleanSymbol,
        side,
        quantity,
        order_style: style,
      }),
    );
    if (receipt) {
      pushToast("Paper order staged", `${text(receipt.status, "ACK")} · ${text(receipt.order_id, "no id")}`, "ok");
      reload();
      return;
    }
    if (order.error) toastError("Order rejected", order.error);
  };

  const cancelAll = async (): Promise<void> => {
    const receipt = await cancel.run(() => postCancelOrders("ALL"));
    if (receipt) {
      pushToast(
        "Cancel sweep complete",
        `${num(receipt.cancelled ?? 0, 0)} cancelled · ${num(receipt.pending_voided ?? 0, 0)} reservations voided`,
        "ok",
      );
      setConfirmOpen(false);
      reload();
      return;
    }
    if (cancel.error) toastError("Cancel sweep rejected", cancel.error);
  };

  const tabTitle = tab === "orders" ? "Execution book" : tab === "positions" ? "Position book" : "Atomic outbox";
  const tabEyebrow =
    tab === "orders" ? "maker-peg · 3.0s hard cancel" : tab === "positions" ? "ledger · 60s reconciliation" : "two-phase · crash atomic";

  return (
    <>
      <SectionHeader
        eyebrow={`ring0 · ${text(data?.page, "orders")} · crash-atomic outbox`}
        title="SmartOMS Orders & Positions"
        subtitle="Maker-Peg and TWAP/Iceberg working orders, the two-phase outbox, and per-silo positions reconciled against the double-entry ledger."
        actions={
          <>
            <StatusPill value={isLive ? "LIVE VAULT" : "PAPER SANDBOX"} tone={isLive ? "bad" : "accent"} />
            <button
              type="button"
              className="btn btn-sm"
              onClick={reload}
              disabled={loading && data === null}
              aria-label="Refresh the order blotter"
            >
              {loading && data === null ? "Syncing…" : "Refresh"}
            </button>
          </>
        }
      />

      {error ? <ErrorBlock message={error} onRetry={reload} /> : null}

      {data === null && loading ? <LoadingBlock rows={4} /> : null}

      {data ? (
        <div className="flex flex-col gap-[14px]">
          <div className="kpi-strip">
            <MetricTile label="Open orders" value={int(working.length)} hint="Maker-Peg chasers resting" />
            <MetricTile label="TWAP / Iceberg" value={int(twaps.length)} hint="slicers in flight" />
            <MetricTile label="Maker-Peg events" value={int(repegs)} hint="posted + re-peg, PEG-tagged" />
            <MetricTile
              label="Outbox pending"
              value={int(outboxPending)}
              hint={`${int(reservationCount)} reservations journalled`}
              tone={outboxPending > 0 ? "warn" : "dim"}
            />
            <MetricTile
              label="Positions"
              value={int(positions.length)}
              hint={
                recon.available
                  ? recon.frozen.size === 0
                    ? "60s guard clear · no drift freeze"
                    : `${int(recon.frozen.size)} silos frozen`
                  : "reconciliation unavailable"
              }
            />
          </div>

          <div className="split-2">
            <GlassPanel title="Order entry" eyebrow={isLive ? "live · signed in ring0" : "paper · ring0 signature"}>
              {isLive ? (
                <div className="rounded-[var(--r-md)] border border-[var(--line-strong)] bg-[var(--panel-sunken)] p-3">
                  <p className="text-[12px] text-[var(--ink-soft)]">
                    Live orders are signed by ring 0 — this surface keeps the synthetic ticket off the live book. Route
                    through the broker adapters and verify the fill in the blotter below.
                  </p>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <div className="grid grid-cols-2 gap-2">
                    <label className="field">
                      <span className="field-label">Silo</span>
                      <select
                        className="select"
                        value={activeSilo}
                        onChange={(event) => setSilo(event.target.value)}
                        aria-label="Order entry silo"
                      >
                        {siloOptions.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span className="field-label">Symbol</span>
                      <input
                        className="input mono"
                        value={symbol}
                        onChange={(event) => setSymbol(event.target.value)}
                        placeholder="BTCUSDT"
                        aria-label="Order entry symbol"
                      />
                    </label>
                  </div>

                  <div className="grid grid-cols-[auto_minmax(0,1fr)] items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <span className="field-label">Side</span>
                      <div className="seg">
                        {(["BUY", "SELL"] as const).map((option) => (
                          <button
                            key={option}
                            type="button"
                            className="seg-item"
                            aria-pressed={side === option}
                            aria-label={`Set order entry side ${option}`}
                            onClick={() => setSide(option)}
                          >
                            {option}
                          </button>
                        ))}
                      </div>
                    </div>
                    <label className="field">
                      <span className="field-label">Quantity</span>
                      <input
                        className="input tnum"
                        inputMode="decimal"
                        value={qty}
                        onChange={(event) => setQty(event.target.value)}
                        aria-label="Order entry quantity"
                      />
                    </label>
                  </div>

                  <div className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-2">
                    <label className="field">
                      <span className="field-label">Order style</span>
                      <select
                        className="select"
                        value={style}
                        onChange={(event) =>
                          setStyle(event.target.value === "TWAP_ICEBERG" ? "TWAP_ICEBERG" : "MAKER_PEG")
                        }
                        aria-label="Order entry style"
                      >
                        <option value="MAKER_PEG">MAKER_PEG</option>
                        <option value="TWAP_ICEBERG">TWAP_ICEBERG</option>
                      </select>
                    </label>
                    <button
                      type="button"
                      className="btn btn-accent"
                      onClick={() => void submitOrder()}
                      disabled={order.busy}
                      aria-label="Submit the paper order"
                    >
                      <Send size={12} aria-hidden="true" />
                      {order.busy ? "Staging…" : "Stage order"}
                    </button>
                  </div>

                  {order.error ? <ErrorBlock message={order.error} /> : null}
                  {order.receipt ? (
                    <div className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-2.5 py-2">
                      <div className="row-between gap-2">
                        <span className="eyebrow">paper receipt</span>
                        <StatusPill value={text(order.receipt.status, "ACK")} tone="ok" />
                      </div>
                      <div className="mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-[var(--ink-soft)]">
                        <span>{text(order.receipt.order_id, "—").slice(0, 10)}</span>
                        <span className="text-[var(--dim)]">·</span>
                        <span>
                          fill <TabularValue value={order.receipt.filled_quantity} kind="num" />
                        </span>
                        <span>
                          @ <TabularValue value={order.receipt.fill_price} kind="price" />
                        </span>
                        <span className="text-[var(--dim)]">·</span>
                        <span>
                          net <TabularValue value={order.receipt.net_bps} kind="bps" digits={1} signTone />
                        </span>
                      </div>
                    </div>
                  ) : null}
                </div>
              )}
            </GlassPanel>

            <GlassPanel title="Cancel all open orders" eyebrow="voids every pending reservation">
              <div className="flex flex-col gap-2">
                <p className="text-[11.5px] text-[var(--muted)]">
                  One sweep cancels every resting Maker-Peg and TWAP/Iceberg remainder, then voids the matching
                  two-phase reservations so no orphaned hold survives the cancel.
                </p>
                <div className="row flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => setConfirmOpen(true)}
                    aria-label="Open the cancel-all confirmation dialog"
                  >
                    <Ban size={12} aria-hidden="true" />
                    Cancel All Open Orders
                  </button>
                  <StatusPill
                    value={`${int(working.length)} working`}
                    tone={working.length > 0 ? "warn" : "neutral"}
                  />
                </div>
                {cancel.error ? <ErrorBlock message={cancel.error} /> : null}
                {cancel.receipt ? (
                  <p className="mono text-[10.5px] text-[var(--pos)]">
                    cancelled {int(cancel.receipt.cancelled ?? 0)} · voided {int(cancel.receipt.pending_voided ?? 0)}
                  </p>
                ) : null}
              </div>
            </GlassPanel>
          </div>

          <div className="row-between flex-wrap gap-2">
            <TabStrip
              tabs={[
                { id: "orders", label: "Orders", count: working.length },
                { id: "positions", label: "Positions", count: positions.length },
                { id: "outbox", label: "Outbox", count: outboxPending },
              ]}
              active={tab}
              onChange={setTab}
              label="Execution book tabs"
            />
            <span className="mono text-[9.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
              reconciled every 60s · drift &gt; 0.01% freezes the silo
            </span>
          </div>

          <GlassPanel title={tabTitle} eyebrow={tabEyebrow}>
            {tab === "orders" ? (
              <div className="flex flex-col gap-3">
                <OrderBookTable rows={working} />
                <div className="border-t border-[var(--line)] pt-2">
                  <div className="row-between mb-1 gap-2">
                    <p className="eyebrow">TWAP / Iceberg slicers</p>
                    <span className="mono text-[9.5px] text-[var(--dim)]">
                      max 10% of top of book per child order
                    </span>
                  </div>
                  <SlicerTable rows={twaps} />
                </div>
              </div>
            ) : tab === "positions" ? (
              <PositionTable rows={positions} recon={recon} />
            ) : (
              <div className="flex flex-col gap-3">
                <div className="kpi-strip">
                  <MetricTile label="Reservations" value={int(reservationCount)} hint="journalled in TigerBeetle" />
                  <MetricTile
                    label="Pending"
                    value={int(outboxPending)}
                    hint="two-phase holds open"
                    tone={outboxPending > 0 ? "warn" : "dim"}
                  />
                  <MetricTile label="Committed" value={int(outboxCommitted)} hint="ledger transfer posted" tone="pos" />
                  <MetricTile label="Voided" value={int(outboxVoided)} hint="hold released" />
                  <MetricTile
                    label="Last outbox write"
                    value={lastLatencyNs === null ? "—" : microLatency(lastLatencyNs / 1000)}
                    hint={budgetNs === null ? "no budget reported" : `budget ${microLatency(budgetNs / 1000)}`}
                  />
                </div>
                <ReservationTable rows={reservations} />
              </div>
            )}
          </GlassPanel>
        </div>
      ) : null}

      <ConfirmDangerModal
        open={confirmOpen}
        title="Cancel every open order"
        body={
          <span>
            Every resting Maker-Peg and TWAP/Iceberg remainder is cancelled, and the matching two-phase outbox
            reservations are voided. Type CANCEL to confirm.
          </span>
        }
        requireText="CANCEL"
        confirmLabel="Cancel All Open Orders"
        busy={cancel.busy}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() => void cancelAll()}
      />
    </>
  );
}
