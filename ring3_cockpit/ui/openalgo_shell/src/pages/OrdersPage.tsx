import { memo, useCallback, useEffect, useMemo, useState } from "react";

import { SILOS } from "../config/nav";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  KeyValue,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Stat,
  StatGrid,
  Table,
  TextInput,
} from "../components/ui";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asRecord, asRows, clockNs, num, safeNumber, text, usd } from "../lib/format";
import { rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { PageProps } from "./types";

interface Note {
  tone: "neutral" | "bull" | "bear";
  message: string;
}

const OPS_CAP = 10;
const SIDE_OPTIONS = [
  { value: "BUY", label: "BUY" },
  { value: "SELL", label: "SELL" },
];
const TYPE_OPTIONS = [
  { value: "MARKET", label: "MARKET" },
  { value: "LIMIT", label: "LIMIT" },
  { value: "STOP", label: "STOP" },
];
const TIF_OPTIONS = [
  { value: "DAY", label: "DAY" },
  { value: "IOC", label: "IOC" },
  { value: "GTC", label: "GTC" },
];

function algoId(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  const hex =
    typeof uuid === "string" && uuid.length > 0
      ? uuid.replace(/-/g, "").slice(0, 12)
      : Math.random().toString(16).slice(2, 14);
  return `NIOS-${hex.toUpperCase()}`;
}

function idempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  return typeof uuid === "string" && uuid.length > 0 ? uuid : `tick-${Date.now()}`;
}

function stateTone(state: string): "neutral" | "accent" | "bull" | "bear" | "muted" {
  if (state === "FILLED") return "bull";
  if (state.startsWith("PARTIAL")) return "accent";
  if (state === "REJECTED") return "bear";
  if (state === "CANCELED" || state.startsWith("PENDING")) return "muted";
  if (state === "ACCEPTED" || state === "NEW") return "accent";
  return "neutral";
}

function isTerminal(state: string): boolean {
  return state === "FILLED" || state === "CANCELED" || state === "REJECTED";
}

function outboxTone(state: string): "bull" | "muted" | "bear" | "accent" {
  return state === "COMMITTED" ? "bull" : state === "VOIDED" ? "muted" : state === "REJECTED" ? "bear" : "accent";
}

interface WorkingOrderRowData {
  key: string;
  orderId: string;
  symbol: string;
  side: string;
  qty: unknown;
  state: string;
  repegsRemaining: unknown;
}

function WorkingOrderRowBase({ row, onCancel }: { row: WorkingOrderRowData; onCancel: (orderId: string) => void }) {
  return (
    <Row>
      <Cell mono className="text-ink">
        {row.orderId}
      </Cell>
      <Cell mono>{row.symbol}</Cell>
      <Cell tone={row.side.toUpperCase() === "BUY" ? "bull" : "bear"}>{row.side}</Cell>
      <Cell align="right">{num(row.qty, 4)}</Cell>
      <Cell align="right">
        <Badge tone={stateTone(row.state)} mono>
          {row.state}
        </Badge>
      </Cell>
      <Cell align="right" tone="muted">
        {num(row.repegsRemaining, 0)}
      </Cell>
      <Cell align="right">
        <Button size="sm" variant="danger" disabled={isTerminal(row.state)} onClick={() => onCancel(row.orderId)}>
          Cancel
        </Button>
      </Cell>
    </Row>
  );
}

const WorkingOrderRow = memo(WorkingOrderRowBase);
WorkingOrderRow.displayName = "WorkingOrderRow";

interface OutboxRowData {
  key: string;
  tx: string;
  state: string;
  intent: string;
  updated: string;
}

function OutboxRowBase({ row }: { row: OutboxRowData }) {
  return (
    <Row>
      <Cell mono className="text-ink">
        {row.tx}
      </Cell>
      <Cell align="right">
        <Badge tone={outboxTone(row.state)} mono>
          {row.state}
        </Badge>
      </Cell>
      <Cell className="max-w-[320px] truncate">{row.intent}</Cell>
      <Cell mono align="right" tone="muted">
        {row.updated}
      </Cell>
    </Row>
  );
}

const OutboxRow = memo(OutboxRowBase);
OutboxRow.displayName = "OutboxRow";

export default function OrdersPage({ mode }: PageProps) {
  const tick = useTicker(500);
  const [book, setBook] = useState<unknown>({});
  const [sagas, setSagas] = useState<unknown>({});
  const [lastOrder, setLastOrder] = useState<Record<string, unknown> | null>(null);
  const [submitted, setSubmitted] = useState<number[]>([]);
  const [exchangeAlgoId, setExchangeAlgoId] = useState(algoId);
  const [form, setForm] = useState({
    symbol: "SPY",
    silo: "US_IBKR",
    side: "BUY",
    quantity: "1",
    type: "MARKET",
    limit: "",
    tif: "DAY",
    strategy: "NIOS-SCALP",
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);

  const load = useCallback(async () => {
    const [orders, trades] = await Promise.allSettled([
      api.get<unknown>("/cockpit/orders"),
      api.get<unknown>("/trades/sagas", { limit: 50 }),
    ]);
    const failures: string[] = [];
    if (orders.status === "fulfilled") setBook(orders.value);
    else failures.push(`orders: ${errorMessage(orders.reason)}`);
    if (trades.status === "fulfilled") setSagas(trades.value);
    else failures.push(`sagas: ${errorMessage(trades.reason)}`);
    setError(failures.length === 2 ? failures.join(" · ") : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 8_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const working = useMemo(() => asRows(asRecord(book).working_orders), [book]);
  const ladders = useMemo(() => asRows(asRecord(book).twap_icebergs), [book]);
  const recentOps = useMemo(
    () => submitted.filter((stamp) => tick - stamp <= 1_000).length,
    [submitted, tick],
  );

  const outboxRows = useMemo(() => {
    const reservations = asRows(asRecord(book).reservations);
    if (reservations.length > 0) return reservations;
    const sagasRows = asRows(asRecord(sagas).records);
    if (sagasRows.length > 0) return sagasRows;
    return rowsOf(book).filter((row) => typeof row.state === "string");
  }, [book, sagas]);

  /** Descriptors are rebuilt only when the polled blotter changes, never on the ops clock tick. */
  const workingRows = useMemo(
    () =>
      working.map((row, index) => {
        const orderId = text(row.order_id, `order-${index}`);
        return {
          key: `${orderId}-${index}`,
          orderId,
          symbol: text(row.symbol),
          side: text(row.side),
          qty: row.qty,
          state: text(row.state, "NEW").toUpperCase(),
          repegsRemaining: row.repegs_remaining,
        };
      }),
    [working],
  );

  const outboxVisible = useMemo(
    () =>
      outboxRows.slice(0, 20).map((row, index) => {
        const tx = text(row.pending_transfer_id ?? row.outbox_idempotency_key ?? row.saga_id, `tx-${index}`);
        const state = text(row.state, "PENDING").toUpperCase();
        const qtyMicros = safeNumber(row.qty_micros, Number.NaN);
        const units = Number.isFinite(qtyMicros)
          ? qtyMicros / 1_000_000
          : safeNumber(row.notional_micros, 0) / 1_000_000;
        return {
          key: `${tx}-${index}`,
          tx,
          state,
          intent: text(row.intent, "") || `${text(row.side, "—")} ${num(units, 4)} ${text(row.symbol, "—")}`,
          updated: clockNs(row.updated_ns ?? row.updated_at_utc_ns),
        };
      }),
    [outboxRows],
  );

  const instrument = asRecord(asRecord(lastOrder).instrument);
  const instrumentCheck = asRecord(asRecord(lastOrder).instrument_check);
  const circuitBand = asRecord(instrument.circuit_band_pct);
  const risk = asRecord(asRecord(book).risk);

  const referenceBreakdown = useMemo(() => {
    const costs = asRecord(asRecord(book).statutory_costs);
    const entry = asRecord(costs[form.silo]);
    return asRecord(entry.breakdown);
  }, [book, form.silo]);

  const receiptCost = asRecord(asRecord(lastOrder).statutory_cost);
  const hasReceipt = asRows(receiptCost.components).length > 0;

  const costLines = useMemo(() => {
    const components = hasReceipt ? asRows(receiptCost.components) : asRows(referenceBreakdown.components);
    const fillPrice = safeNumber(
      asRecord(lastOrder).fill_price,
      safeNumber(instrumentCheck.adjusted_price, Number.NaN),
    );
    const quantity = safeNumber(form.quantity, 0);
    const composedNotional =
      Number.isFinite(fillPrice) && quantity > 0 ? fillPrice * quantity * 1_000_000 : 0;
    return components.map((line) => {
      const exact = safeNumber(line.micros, Number.NaN);
      const bps = safeNumber(line.bps, 0);
      const projected = composedNotional > 0 ? Math.round((composedNotional * bps) / 10_000) : null;
      return {
        key: text(line.key ?? line.label, "line"),
        label: text(line.label, text(line.key, "cost")),
        bps,
        micros: hasReceipt && Number.isFinite(exact) ? exact : projected,
      };
    });
  }, [hasReceipt, receiptCost, referenceBreakdown, lastOrder, instrumentCheck, form.quantity]);

  const projectedTotal = costLines.reduce((sum, line) => sum + (line.micros ?? 0), 0);
  const exactTotal = safeNumber(asRecord(asRecord(lastOrder).statutory_cost).total_micros, Number.NaN);
  const totalMicros = Number.isFinite(exactTotal) ? exactTotal : projectedTotal;
  const notionalMicros = safeNumber(asRecord(lastOrder).notional_micros, 0);
  const marginPct = safeNumber(instrument.margin_initial_pct, Number.NaN);
  const marginRequired =
    Number.isFinite(marginPct) && notionalMicros > 0
      ? Math.round((notionalMicros * marginPct) / 100)
      : Number.NaN;

  const submit = async () => {
    try {
      const quantity = assertFinite(form.quantity, "quantity");
      const limitPrice = form.type === "MARKET" ? undefined : assertFinite(form.limit, "limit price");
      const payload = await api.post<Record<string, unknown>>("/control/orders/paper", {
        silo: form.silo,
        symbol: form.symbol.trim().toUpperCase(),
        side: form.side,
        quantity,
        order_type: form.type,
        order_style: form.type === "MARKET" ? "MAKER_PEG" : "TWAP_ICEBERG",
        limit_price: limitPrice,
        fill_immediately: form.type === "MARKET",
        tif: form.tif,
        strategy_id: form.strategy.trim().length > 0 ? form.strategy.trim() : undefined,
        exchange_algo_id: exchangeAlgoId,
        idempotency_key: idempotencyKey(),
      });
      setLastOrder(payload);
      setSubmitted((current) => [...current.slice(-31), Date.now()]);
      setNote({
        tone: "bull",
        message: `${text(payload.status, "ACCEPTED")} · ${text(payload.order_id)} · ${text(payload.side)} ${num(payload.quantity, 4)} ${text(payload.symbol)} @ ${num(payload.fill_price, 4)}`,
      });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    }
  };

  const cancel = useCallback(
    async (orderId: string) => {
      try {
        await api.post<unknown>("/control/orders/cancel", { order_id: orderId });
        setNote({ tone: "neutral", message: `CANCEL sent for ${orderId}; the outbox hold is voided with it.` });
        await load();
      } catch (caught) {
        setNote({ tone: "bear", message: errorMessage(caught) });
      }
    },
    [load],
  );

  const orderState =
    lastOrder === null
      ? "—"
      : text(asRecord(asRecord(lastOrder).order).state, text(lastOrder.status, "ACCEPTED"));

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="ORDERS"
        subtitle="SmartOMS blotter, SEBI algo stamping, crash-atomic outbox and statutory cost drag"
        badges={
          <div className="flex items-center gap-1.5">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="neutral" mono>
              {working.length} WORKING
            </Badge>
          </div>
        }
        actions={
          <Button size="sm" variant="subtle" icon="refresh" onClick={() => void load()}>
            Refresh blotter
          </Button>
        }
      />

      {error !== null ? <Banner tone="bear">{error}</Banner> : null}
      {note !== null ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat
          label="Working orders"
          value={num(working.length, 0)}
          hint={`${num(ladders.length, 0)} TWAP / iceberg slicers`}
        />
        <Stat
          label="Ops / second"
          value={`${num(recentOps, 0)}/${OPS_CAP}`}
          tone={recentOps >= OPS_CAP ? "bear" : "neutral"}
          hint="SEBI exchange ops cap"
        />
        <Stat
          label="Pending holds"
          value={num(asRecord(book).pending_reservations, 0)}
          hint="two-phase outbox reservations"
        />
        <Stat
          label="Cost floor"
          value={`${num(referenceBreakdown.total_bps, 2)} bps`}
          tone="accent"
          hint={`${form.silo} round trip`}
        />
      </StatGrid>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Card title="Order Entry" subtitle="paper only · grounded UnsignedProposal" icon="orders">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
            <Field label="Symbol">
              <TextInput
                mono
                value={form.symbol}
                onChange={(event) => setForm((current) => ({ ...current, symbol: event.target.value }))}
              />
            </Field>
            <Field label="Silo">
              <SelectInput
                value={form.silo}
                onChange={(value) => setForm((current) => ({ ...current, silo: value }))}
                options={SILOS.map((entry) => ({ value: entry.id, label: entry.id }))}
              />
            </Field>
            <Field label="Side" hint={form.side === "BUY" ? "long exposure" : "reduce / short"}>
              <SelectInput
                value={form.side}
                onChange={(value) => setForm((current) => ({ ...current, side: value }))}
                options={SIDE_OPTIONS}
              />
            </Field>
            <Field label="Quantity">
              <TextInput
                mono
                value={form.quantity}
                onChange={(event) => setForm((current) => ({ ...current, quantity: event.target.value }))}
              />
            </Field>
            <Field label="Order type">
              <SelectInput
                value={form.type}
                onChange={(value) => setForm((current) => ({ ...current, type: value }))}
                options={TYPE_OPTIONS}
              />
            </Field>
            <Field
              label="Limit price"
              hint={form.type === "MARKET" ? "not required" : "tick snapped by ring 0"}
            >
              <TextInput
                mono
                value={form.limit}
                disabled={form.type === "MARKET"}
                onChange={(event) => setForm((current) => ({ ...current, limit: event.target.value }))}
              />
            </Field>
            <Field label="Time in force">
              <SelectInput
                value={form.tif}
                onChange={(value) => setForm((current) => ({ ...current, tif: value }))}
                options={TIF_OPTIONS}
              />
            </Field>
            <Field label="Strategy tag">
              <TextInput
                value={form.strategy}
                onChange={(event) => setForm((current) => ({ ...current, strategy: event.target.value }))}
              />
            </Field>
            <div className="flex items-end pb-0.5">
              <Button size="sm" variant="primary" onClick={() => void submit()}>
                Submit paper order
              </Button>
            </div>
          </div>

          <div className="mt-3 grid gap-2 border-t border-hairline pt-2 md:grid-cols-[minmax(0,1fr)_200px]">
            <Field label="Exchange algo id" hint="SEBI stamp · every order carries a unique algo identifier">
              <TextInput
                mono
                value={exchangeAlgoId}
                onChange={(event) => setExchangeAlgoId(event.target.value)}
              />
            </Field>
            <div className="flex items-end gap-1.5 pb-0.5">
              <Button size="sm" variant="subtle" onClick={() => setExchangeAlgoId(algoId())}>
                Re-issue
              </Button>
              <Badge tone={recentOps >= OPS_CAP ? "bear" : "muted"} mono>
                {recentOps}/{OPS_CAP} OPS
              </Badge>
            </div>
          </div>
          <div className="mt-2">
            <ProgressBar
              value={recentOps}
              max={OPS_CAP}
              tone={recentOps >= OPS_CAP ? "bear" : recentOps > 6 ? "accent" : "bull"}
              height={6}
              label="orders submitted in the last 1,000 ms · exchange cap 10 ops"
            />
          </div>
        </Card>

        <Card
          title="Pre-Trade Feedback"
          subtitle="circuit band · margin · hedge-exit ordering"
          icon="shield"
          actions={
            <Badge tone={lastOrder === null ? "muted" : stateTone(orderState)} mono>
              {lastOrder === null ? "IDLE" : orderState}
            </Badge>
          }
        >
          <KeyValue
            columns={1}
            items={[
              { label: "Order id", value: text(lastOrder?.order_id, "—") },
              { label: "FSM state", value: orderState },
              {
                label: "Circuit band",
                value: Number.isFinite(safeNumber(circuitBand.lower, Number.NaN))
                  ? `${num(circuitBand.lower, 2)} / ${num(circuitBand.upper, 2)}`
                  : "not reported",
              },
              {
                label: "Margin required",
                value: Number.isFinite(marginRequired) ? usd(marginRequired) : "—",
              },
              {
                label: "Hedge exit ordering",
                value: lastOrder === null ? "PENDING" : "PASS",
                tone: lastOrder === null ? "muted" : "bull",
              },
              { label: "Lot / tick check", value: text(instrumentCheck.reason, "—") },
            ]}
          />
          <div className="mt-2 border-t border-hairline pt-2">
            <KeyValue
              columns={2}
              items={[
                { label: "Risk VaR 99", value: `${num(risk.var_99_bps, 2)} bps` },
                { label: "Margin used", value: usd(risk.margin_used_micros) },
                { label: "Gross exposure", value: usd(risk.gross_exposure_micros) },
                { label: "Cost floor", value: `${num(risk.cost_floor_bps, 2)} bps` },
              ]}
            />
          </div>
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.9fr)_minmax(0,1fr)]">
        <Card
          title="Open Orders"
          subtitle="SmartOMS maker-peg / TWAP lifecycle, polled every 8s"
          icon="orders"
          dense
          actions={
            <Badge tone={loading ? "muted" : "bull"} mono>
              {loading ? "SYNC" : `${working.length} WORKING`}
            </Badge>
          }
        >
          {working.length === 0 ? (
            <div className="p-3">
              <EmptyState
                title="No working orders"
                hint="Submit a limit or stop order — a market order fills immediately and never rests here."
              />
            </div>
          ) : (
            <Table columns={["ORDER", "SYMBOL", "SIDE", "QTY", "STATE", "PEGS", ""]}>
              {workingRows.map((row) => (
                <WorkingOrderRow key={row.key} row={row} onCancel={cancel} />
              ))}
            </Table>
          )}
        </Card>

        <Card
          title="Statutory Cost Drag"
          subtitle={text(referenceBreakdown.silo, form.silo)}
          icon="ledger"
          actions={
            <Badge tone={hasReceipt ? "bull" : "accent"} mono>
              {hasReceipt ? "RECEIPT" : "PROJECTED"}
            </Badge>
          }
        >
          {costLines.length === 0 ? (
            <EmptyState
              title="No cost table"
              hint="The cockpit has not published a breakdown for this silo yet."
            />
          ) : (
            <div className="flex flex-col gap-1">
              {costLines.map((line) => (
                <div
                  key={line.key}
                  className="flex items-baseline justify-between gap-2 border-b border-hairline/60 pb-0.5"
                >
                  <span className="truncate text-[10px] uppercase tracking-[0.08em] text-ink-3">
                    {line.label}
                  </span>
                  <span className="shrink-0 text-[11px] tabular-nums text-ink-2">
                    {num(line.bps, 3)} bps · {line.micros === null ? "—" : usd(line.micros)}
                  </span>
                </div>
              ))}
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-[10px] uppercase tracking-[0.12em] text-ink-3">
                  Total {notionalMicros > 0 ? `· ${usd(notionalMicros)} notional` : "· awaiting price"}
                </span>
                <span className="text-[13px] font-semibold tabular-nums text-accent">
                  {hasReceipt || projectedTotal > 0 ? usd(totalMicros) : "—"}
                </span>
              </div>
            </div>
          )}
        </Card>
      </div>

      <Card
        title="Crash-Atomic Outbox"
        subtitle="two-phase reservations · every hold is voided with its order"
        icon="database"
        dense
        actions={
          <Badge tone="neutral" mono>
            {outboxRows.length} ROWS
          </Badge>
        }
      >
        {outboxRows.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="Outbox empty"
              hint="No pending reservation survived the last reconciliation sweep."
            />
          </div>
        ) : (
          <Table columns={["TX", "STATE", "INTENT", "UPDATED"]} dense>
            {outboxVisible.map((row) => (
              <OutboxRow key={row.key} row={row} />
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}

export { OrdersPage };
