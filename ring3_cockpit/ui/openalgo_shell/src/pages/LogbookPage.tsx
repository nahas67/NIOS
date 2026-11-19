/**
 * `/logbook` — crash-safe execution journal with tax-net cost attribution.
 *
 * Gross P&L, the itemised statutory drag and net P&L are shown side by side so
 * a winning trade that loses money after costs can never look profitable.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { BarChart } from "../charts";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  Checkbox,
  EmptyState,
  Field,
  Modal,
  PageHeader,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextInput,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, errorMessage } from "../lib/api";
import { asRecord, clockNs, int, num, safeNumber, text, usd, usdSigned } from "../lib/format";
import { numberAt, rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const SILO_OPTIONS = SILOS.map((silo) => ({ value: silo.id, label: `${silo.short} · ${silo.id}` }));
const SIDE_FILTERS = [
  { value: "ALL", label: "ALL SIDES" },
  { value: "LONG", label: "LONG" },
  { value: "SHORT", label: "SHORT" },
];
const OUTCOME_FILTERS = [
  { value: "ALL", label: "ALL OUTCOMES" },
  { value: "WIN", label: "WIN" },
  { value: "LOSS", label: "LOSS" },
];
const BROKERAGE_LEGS = ["brokerage_bps", "commission_bps", "maker_taker_fee_bps"];
const TAX_LEGS = ["stt_bps", "ctt_bps", "sec_section31_bps", "finra_taf_bps", "occ_clearing_bps"];
const EXCHANGE_LEGS = ["exchange_txn_bps", "sebi_turnover_bps"];
const GST_LEGS = ["gst_18pct_bps"];
const STAMP_LEGS = ["stamp_duty_bps"];

function legMicros(components: Record<string, unknown>[], keys: string[]): number {
  let total = 0;
  for (const component of components) {
    if (keys.includes(text(component.key, ""))) total += safeNumber(component.micros, 0);
  }
  return total;
}

function dayOf(entry: Record<string, unknown>): string {
  const ns = numberAt(entry, ["recorded_at_ns"], 0);
  if (!Number.isFinite(ns) || ns <= 0) return "";
  return new Date(ns / 1e6).toISOString().slice(0, 10);
}

function toMicros(value: unknown): number {
  return Math.round(safeNumber(value, 0) * 1_000_000);
}

export function LogbookPage({ mode }: PageProps) {
  const [entries, setEntries] = useState<Record<string, unknown>[]>([]);
  const [summary, setSummary] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [symbolQuery, setSymbolQuery] = useState("");
  const [side, setSide] = useState("ALL");
  const [outcome, setOutcome] = useState("ALL");
  const [fromDay, setFromDay] = useState("");
  const [toDay, setToDay] = useState("");
  const [entryOpen, setEntryOpen] = useState(false);
  const [clearArmed, setClearArmed] = useState(false);
  const [clearPhrase, setClearPhrase] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const now = useTicker(1000);

  const [form, setForm] = useState<{
    silo: string;
    symbol: string;
    side: string;
    quantity: string;
    entry: string;
    exit: string;
    notes: string;
  }>({
    silo: SILOS[0].id,
    symbol: "",
    side: "LONG",
    quantity: "",
    entry: "",
    exit: "",
    notes: "",
  });

  const load = useCallback(async () => {
    try {
      const payload = asRecord(await api.get<unknown>("/cockpit/logbook"));
      setEntries(rowsOf(payload.entries, ["entries"]));
      setSummary(asRecord(payload.aggregate));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 10_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const filtered = useMemo(() => {
    const needle = symbolQuery.trim().toUpperCase();
    return entries.filter((entry) => {
      const symbol = text(entry.symbol, "").toUpperCase();
      if (needle.length > 0 && !symbol.includes(needle)) return false;
      if (side !== "ALL" && text(entry.side, "").toUpperCase() !== side) return false;
      if (outcome !== "ALL" && text(entry.outcome, "").toUpperCase() !== outcome) return false;
      const day = dayOf(entry);
      if (fromDay.length > 0 && day.length > 0 && day < fromDay) return false;
      if (toDay.length > 0 && day.length > 0 && day > toDay) return false;
      return true;
    });
  }, [entries, symbolQuery, side, outcome, fromDay, toDay]);

  const stats = useMemo(() => {
    let gross = 0;
    let cost = 0;
    let net = 0;
    let wins = 0;
    let rSum = 0;
    let grossProfit = 0;
    let grossLoss = 0;
    for (const entry of filtered) {
      const rowGross = safeNumber(entry.gross_pnl, 0);
      const rowCost = safeNumber(entry.cost_drag, 0);
      const rowNet = safeNumber(entry.net_pnl, rowGross - rowCost);
      gross += rowGross;
      cost += rowCost;
      net += rowNet;
      rSum += safeNumber(entry.r_multiple, 0);
      if (rowNet > 0) {
        wins += 1;
        grossProfit += rowNet;
      } else if (rowNet < 0) {
        grossLoss += Math.abs(rowNet);
      }
    }
    return {
      trades: filtered.length,
      winRate: filtered.length > 0 ? (wins / filtered.length) * 100 : 0,
      avgR: filtered.length > 0 ? rSum / filtered.length : 0,
      gross,
      cost,
      net,
      profitFactor: grossLoss > 0 ? grossProfit / grossLoss : null,
    };
  }, [filtered]);

  const bars = useMemo(
    () =>
      filtered.slice(-30).map((entry) => {
        const value = toMicros(entry.net_pnl);
        return {
          label: text(entry.symbol, "?"),
          value,
          tone: value >= 0 ? ("bull" as const) : ("bear" as const),
        };
      }),
    [filtered],
  );

  const submitEntry = async () => {
    setNote(null);
    setBusy("entry");
    try {
      const quantity = Number(form.quantity);
      const entryPrice = Number(form.entry);
      const exitPrice = Number(form.exit);
      if (form.symbol.trim().length === 0) throw new Error("symbol is required");
      if (!Number.isFinite(quantity) || quantity <= 0) throw new Error("quantity must be greater than 0");
      if (!Number.isFinite(entryPrice) || entryPrice <= 0)
        throw new Error("entry price must be greater than 0");
      if (!Number.isFinite(exitPrice) || exitPrice <= 0) throw new Error("exit price must be greater than 0");
      await api.post("/control/logbook/entry", {
        silo: form.silo,
        symbol: form.symbol.trim().toUpperCase(),
        side: form.side,
        setup_strategy: "manual.operator",
        entry_price: entryPrice,
        exit_price: exitPrice,
        quantity,
        stop_loss: 0,
        thesis: form.notes,
        lessons_learned: "",
        workspace: mode,
      });
      setNote({
        tone: "bull",
        message: `LOGBOOK ENTRY APPENDED · ${form.symbol.trim().toUpperCase()} · costs applied by the statutory engine`,
      });
      setEntryOpen(false);
      setForm((current) => ({ ...current, symbol: "", quantity: "", entry: "", exit: "", notes: "" }));
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const clearJournal = async () => {
    setNote(null);
    setBusy("clear");
    try {
      if (!clearArmed || clearPhrase.trim().toUpperCase() !== "CLEAR") {
        throw new Error("clear requires the confirmation checkbox and the typed word CLEAR");
      }
      const payload = asRecord(await api.post<unknown>("/control/logbook/clear", {}));
      setNote({
        tone: "bull",
        message: `LOGBOOK CLEARED · ${int(payload.cleared)} entries removed · sequence reset to 0`,
      });
      setClearArmed(false);
      setClearPhrase("");
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const canClear = clearArmed && clearPhrase.trim().toUpperCase() === "CLEAR" && busy === null;

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="LOGBOOK"
        subtitle="Crash-safe execution journal · tax-net cost attribution · R-multiples"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"}>{mode}</Badge>
            <Badge tone="muted" mono>
              {int(filtered.length)}/{int(entries.length)} ROWS
            </Badge>
          </>
        }
        actions={
          <>
            <Button icon="plus" variant="subtle" size="sm" onClick={() => setEntryOpen(true)}>
              Manual Entry
            </Button>
            <Button icon="refresh" variant="subtle" size="sm" onClick={() => void load()}>
              Refresh
            </Button>
          </>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat
          label="Trades"
          value={int(stats.trades)}
          hint={`journal ${int(summary.entries ?? entries.length)}`}
        />
        <Stat
          label="Win Rate"
          value={`${num(stats.winRate, 1)}%`}
          tone={stats.winRate >= 50 ? "bull" : "bear"}
        />
        <Stat label="Avg R" value={num(stats.avgR, 2)} tone={stats.avgR >= 0 ? "bull" : "bear"} />
        <Stat
          label="Gross"
          value={usdSigned(toMicros(stats.gross))}
          tone={stats.gross >= 0 ? "bull" : "bear"}
        />
        <Stat label="Cost Drag" value={usd(toMicros(stats.cost))} tone="bear" hint="statutory only" />
        <Stat label="Net" value={usdSigned(toMicros(stats.net))} tone={stats.net >= 0 ? "bull" : "bear"} />
        <Stat
          label="Profit Factor"
          value={stats.profitFactor === null ? "∞" : num(stats.profitFactor, 2)}
          tone={stats.profitFactor === null || stats.profitFactor >= 1 ? "bull" : "bear"}
        />
      </StatGrid>

      <Card
        title="Filters"
        subtitle="Client-side slice · the journal is never mutated by a filter"
        icon="filter"
      >
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Symbol" className="w-40">
            <TextInput
              value={symbolQuery}
              onChange={(event) => setSymbolQuery(event.target.value)}
              placeholder="BTC"
              mono
            />
          </Field>
          <Field label="Side" className="w-36">
            <SelectInput value={side} onChange={setSide} options={SIDE_FILTERS} />
          </Field>
          <Field label="Outcome" className="w-40">
            <SelectInput value={outcome} onChange={setOutcome} options={OUTCOME_FILTERS} />
          </Field>
          <Field label="From" className="w-40">
            <TextInput type="date" value={fromDay} onChange={(event) => setFromDay(event.target.value)} />
          </Field>
          <Field label="To" className="w-40">
            <TextInput type="date" value={toDay} onChange={(event) => setToDay(event.target.value)} />
          </Field>
          <Button
            variant="ghost"
            size="sm"
            icon="close"
            onClick={() => {
              setSymbolQuery("");
              setSide("ALL");
              setOutcome("ALL");
              setFromDay("");
              setToDay("");
            }}
          >
            Reset
          </Button>
          <span className="ml-auto text-[10px] uppercase tracking-[0.1em] text-ink-3">
            {clockNs(now * 1e6)} UTC
          </span>
        </div>
      </Card>

      <Card
        title="Net P&L Per Trade"
        subtitle="Last 30 rows in the current slice · bull = profitable after statutory cost"
        icon="activity"
      >
        <BarChart bars={bars} height={160} format={(value) => usd(value)} />
      </Card>

      <Card title="Execution Journal" subtitle="GROSS → itemised statutory drag → NET" icon="logbook" dense>
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> LOADING LOGBOOK…
          </div>
        ) : filtered.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO TRADES IN THIS SLICE"
              hint="Log a PAPER trade or clear the filters. A new install starts on a clean slate with zero entries."
            />
          </div>
        ) : (
          <Table
            dense
            columns={[
              "WHEN",
              "SYMBOL",
              "SIDE",
              "QTY",
              "ENTRY",
              "EXIT",
              "GROSS",
              "BROKERAGE",
              "TAX",
              "EXCHANGE",
              "GST",
              "STAMP",
              "OTHER",
              "COST",
              "NET",
              "R",
              "TAGS",
            ]}
          >
            {filtered.map((entry, index) => {
              const statutory = asRecord(entry.statutory_cost);
              const components = rowsOf(statutory.components, ["components"]);
              const costMicros = numberAt(entry, ["cost_drag_micros"], toMicros(entry.cost_drag));
              const grossMicros = toMicros(entry.gross_pnl);
              const netMicros = toMicros(entry.net_pnl);
              const rMultiple = safeNumber(entry.r_multiple, 0);
              const brokerage = legMicros(components, BROKERAGE_LEGS);
              const tax = legMicros(components, TAX_LEGS);
              const exchange = legMicros(components, EXCHANGE_LEGS);
              const gst = legMicros(components, GST_LEGS);
              const stamp = legMicros(components, STAMP_LEGS);
              const other = Math.max(0, costMicros - brokerage - tax - exchange - gst - stamp);
              return (
                <Row
                  key={`${text(entry.entry_id, "entry")}-${index}`}
                  tone={netMicros > 0 ? "bull" : netMicros < 0 ? "bear" : undefined}
                >
                  <Cell mono className="whitespace-nowrap">
                    {clockNs(entry.recorded_at_ns)}
                  </Cell>
                  <Cell mono className="font-semibold text-ink">
                    {text(entry.symbol)}
                  </Cell>
                  <Cell>
                    <Badge tone={text(entry.side, "").toUpperCase() === "SHORT" ? "bear" : "bull"}>
                      {text(entry.side, "—")}
                    </Badge>
                  </Cell>
                  <Cell align="right">{num(entry.quantity, 2)}</Cell>
                  <Cell align="right" mono>
                    {num(entry.entry_price, 2)}
                  </Cell>
                  <Cell align="right" mono>
                    {num(entry.exit_price, 2)}
                  </Cell>
                  <Cell align="right" tone={grossMicros >= 0 ? "bull" : "bear"}>
                    {usdSigned(grossMicros)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(brokerage)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(tax)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(exchange)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(gst)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(stamp)}
                  </Cell>
                  <Cell align="right" tone="muted">
                    {usd(other)}
                  </Cell>
                  <Cell align="right" tone="bear">
                    {usd(costMicros)}
                  </Cell>
                  <Cell align="right" tone={netMicros >= 0 ? "bull" : "bear"} className="font-semibold">
                    {usdSigned(netMicros)}
                  </Cell>
                  <Cell align="right" tone={rMultiple >= 1 ? "bull" : rMultiple < 0 ? "bear" : "muted"}>
                    {num(rMultiple, 2)}
                  </Cell>
                  <Cell className="text-[9.5px] text-ink-3">
                    <div className="flex flex-wrap items-center gap-1">
                      <Badge tone={text(entry.outcome, "").toUpperCase() === "WIN" ? "bull" : "muted"}>
                        {text(entry.outcome, "—")}
                      </Badge>
                      <span className="font-mono">{text(entry.setup_strategy, "—")}</span>
                      <span>{text(entry.workspace, "")}</span>
                    </div>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <Card
        title="Destructive · Clear Journal"
        subtitle="Removes every entry and resets the sequence · not reversible"
        icon="warning"
      >
        <div className="flex flex-wrap items-end gap-3">
          <Checkbox
            checked={clearArmed}
            onChange={setClearArmed}
            label="I understand the journal, its sequence and every R-multiple are deleted."
          />
          <Field label="Type CLEAR" className="w-40">
            <TextInput value={clearPhrase} onChange={(event) => setClearPhrase(event.target.value)} mono />
          </Field>
          <Button variant="danger" icon="warning" disabled={!canClear} onClick={() => void clearJournal()}>
            {busy === "clear" ? "CLEARING…" : "Clear Journal"}
          </Button>
        </div>
      </Card>

      <Modal
        open={entryOpen}
        title="Manual Logbook Entry"
        onClose={() => setEntryOpen(false)}
        width={560}
        footer={
          <>
            <Button variant="ghost" onClick={() => setEntryOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              icon="check"
              disabled={busy !== null}
              onClick={() => void submitEntry()}
            >
              {busy === "entry" ? "APPENDING…" : "Append Entry"}
            </Button>
          </>
        }
      >
        <div className="grid grid-cols-2 gap-2">
          <Field label="Silo" className="col-span-2">
            <SelectInput
              value={form.silo}
              onChange={(value) => setForm((current) => ({ ...current, silo: value }))}
              options={SILO_OPTIONS}
            />
          </Field>
          <Field label="Symbol" hint="uppercase, 1-32 chars">
            <TextInput
              value={form.symbol}
              onChange={(event) => setForm((current) => ({ ...current, symbol: event.target.value }))}
              placeholder="BTCUSDT"
              mono
            />
          </Field>
          <Field label="Side">
            <SelectInput
              value={form.side}
              onChange={(value) => setForm((current) => ({ ...current, side: value }))}
              options={[
                { value: "LONG", label: "LONG" },
                { value: "SHORT", label: "SHORT" },
              ]}
            />
          </Field>
          <Field label="Quantity" hint="> 0">
            <TextInput
              value={form.quantity}
              onChange={(event) => setForm((current) => ({ ...current, quantity: event.target.value }))}
              inputMode="decimal"
              mono
            />
          </Field>
          <Field label="Fees" hint="engine-computed — never operator-entered">
            <TextInput value="AUTO · STATUTORY" disabled mono />
          </Field>
          <Field label="Entry Price" hint="> 0">
            <TextInput
              value={form.entry}
              onChange={(event) => setForm((current) => ({ ...current, entry: event.target.value }))}
              inputMode="decimal"
              mono
            />
          </Field>
          <Field label="Exit Price" hint="> 0">
            <TextInput
              value={form.exit}
              onChange={(event) => setForm((current) => ({ ...current, exit: event.target.value }))}
              inputMode="decimal"
              mono
            />
          </Field>
          <Field label="Notes" className="col-span-2" hint="stored as the thesis, max 4000 chars">
            <TextInput
              value={form.notes}
              onChange={(event) => setForm((current) => ({ ...current, notes: event.target.value }))}
              placeholder="Why this trade was taken"
            />
          </Field>
        </div>
        <p className="mt-2 text-[10px] leading-snug text-ink-3">
          The statutory engine derives cost drag, outcome, net bps and the R-multiple from the four prices.
          Workspace is fixed to {mode}.
        </p>
      </Modal>
    </div>
  );
}

export default LogbookPage;
