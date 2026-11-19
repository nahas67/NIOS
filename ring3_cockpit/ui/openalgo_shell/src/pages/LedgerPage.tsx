/**
 * `/ledger` — IBOR double-entry journal, silo x vault custody grid, tax escrow,
 * Finance Act 2026 itemised statutory cost and the 4-silo reconciler.
 *
 * Ring 0 is the only writer. This page reads; the two mutations it owns are the
 * PAPER capital deposit and the read-only reconciliation sweep.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { EquityDrawdownCurve } from "../charts";
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
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextInput,
} from "../components/ui";
import { SILOS, VAULTS } from "../config/nav";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asRecord, clockNs, int, num, pct, safeNumber, text, toneForNumber, usd } from "../lib/format";
import { numberAt, rowsOf } from "../lib/payload";
import type { ApiNote, PageProps } from "./types";

const SILO_OPTIONS = SILOS.map((silo) => ({ value: silo.id, label: `${silo.short} · ${silo.id}` }));
const TAX_ESCROW_ACCOUNT = "FEE_TAX_ACCRUAL";
const TRANSFER_CODES: Record<string, string> = {
  "1": "FUNDING",
  "2": "ORDER_RESERVATION",
  "3": "FILL_SETTLEMENT",
  "4": "CHARGE_ACCRUAL",
  "5": "PROFIT_RATCHET",
  "6": "RECONCILIATION_REPAIR",
};
const TAX_LEGS = ["stt_bps", "ctt_bps", "sec_section31_bps", "finra_taf_bps", "occ_clearing_bps"];
const EXCHANGE_LEGS = ["exchange_txn_bps", "sebi_turnover_bps"];
const VENUE_LEGS = [
  "brokerage_bps",
  "maker_taker_fee_bps",
  "funding_rate_8h_bps",
  "liquidation_insurance_bps",
  "commission_bps",
  "raw_spread_bps",
  "ecn_commission_bps",
  "tom_next_swap_bps",
];
const STAMP_LEGS = ["stamp_duty_bps"];
const GST_LEGS = ["gst_18pct_bps"];
const SLIPPAGE_LEGS = ["modeled_slippage_bps"];

function accountParts(account: unknown): { silo: string; code: string } {
  const token = text(account, "—");
  const separator = token.indexOf(":");
  if (separator < 0) return { silo: "", code: token };
  return { silo: token.slice(0, separator), code: token.slice(separator + 1) };
}

function legMicros(components: Record<string, unknown>[], keys: string[]): number {
  let total = 0;
  for (const component of components) {
    if (keys.includes(text(component.key, ""))) total += safeNumber(component.micros, 0);
  }
  return total;
}

export function LedgerPage({ mode }: PageProps) {
  const [ledger, setLedger] = useState<Record<string, unknown>>({});
  const [performance, setPerformance] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [fundSilo, setFundSilo] = useState<string>(SILOS[0].id);
  const [fundAmount, setFundAmount] = useState("100000");
  const [busy, setBusy] = useState<string | null>(null);
  const [reconciled, setReconciled] = useState<Record<string, Record<string, unknown>>>({});

  const load = useCallback(async () => {
    try {
      const [journal, analytics] = await Promise.all([
        api.get<unknown>("/cockpit/ledger"),
        api.get<unknown>("/analytics/performance"),
      ]);
      setLedger(asRecord(journal));
      setPerformance(asRecord(analytics));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 12_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const journal = useMemo(() => rowsOf(ledger.transfers, ["transfers", "rows"]), [ledger]);
  const invariant = useMemo(() => asRecord(ledger.invariant), [ledger]);
  const balances = useMemo(() => asRecord(invariant.balances), [invariant]);
  const risk = useMemo(() => asRecord(ledger.risk), [ledger]);
  const silos = useMemo(() => asRecord(ledger.silos), [ledger]);
  const statutory = useMemo(() => asRecord(ledger.statutory_costs), [ledger]);
  const attribution = useMemo(() => rowsOf(performance.by_silo, ["by_silo"]), [performance]);

  const totals = useMemo(() => {
    let debits = 0;
    let credits = 0;
    for (const row of journal) {
      const amount = numberAt(row, ["amount_micros"], 0);
      if (text(row.debit_account, "").length > 0) debits += amount;
      if (text(row.credit_account, "").length > 0) credits += amount;
    }
    return { debits, credits, delta: debits - credits };
  }, [journal]);

  const invariantHolds = invariant.holds === undefined ? true : invariant.holds === true;
  const balanced = totals.debits === totals.credits && invariantHolds;
  const navMicros = numberAt(ledger.counts, ["nav_micros"], 0);

  const equity = useMemo(
    () => rowsOf(performance.equity_curve, ["equity_curve"]).map((row) => numberAt(row, ["nav_micros"], 0)),
    [performance],
  );
  const drawdown = useMemo(
    () => rowsOf(performance.underwater, ["underwater"]).map((row) => numberAt(row, ["drawdown_pct"], 0)),
    [performance],
  );

  const deposit = async () => {
    setNote(null);
    setBusy("deposit");
    try {
      const units = assertFinite(Number(fundAmount), "paper capital");
      if (units <= 0) throw new Error("amount must be greater than 0 units");
      await api.post("/control/ledger/fund-paper", {
        silo: fundSilo,
        amount_units: units,
        vault: "ALL_CANONICAL",
      });
      setNote({
        tone: "bull",
        message: `PAPER CAPITAL FUNDED · ${fundSilo} · ${units} units · ALL_CANONICAL`,
      });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const reconcile = async (siloId: string) => {
    setNote(null);
    setBusy(siloId);
    try {
      const payload = await api.post<unknown>("/control/ledger/reconcile", {
        silos: [siloId],
        silo: siloId,
        operator: "ring3.operator",
      });
      const report = asRecord(payload);
      const row = asRecord(asRecord(report.silos)[siloId]);
      setReconciled((current) => ({ ...current, [siloId]: row }));
      const frozen = row.frozen === true || row.breached === true;
      setNote({
        tone: frozen ? "bear" : "bull",
        message: `${siloId} · ${text(report.status, frozen ? "DRIFT_BREACH" : "RECONCILED")} · ${num(
          numberAt(row, ["drift_ppm"], 0) / 100,
          4,
        )} bps drift · ${frozen ? "FREEZE" : "BALANCED"}`,
      });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="LEDGER"
        subtitle="Double-entry IBOR · 4 silos × 4 vaults · tax escrow · Finance Act 2026 cost floors"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"}>{mode}</Badge>
            <Badge tone={balanced ? "bull" : "bear"} mono>
              {balanced ? "BALANCED" : "DRIFT"}
            </Badge>
          </>
        }
        actions={
          <Button icon="refresh" variant="subtle" size="sm" onClick={() => void load()}>
            Refresh
          </Button>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={5}>
        <Stat label="NAV" value={usd(navMicros)} hint={`${int(navMicros)} micros`} />
        <Stat label="Journal Rows" value={int(journal.length)} hint="2-phase outbox transfers" />
        <Stat label="Debits" value={int(totals.debits)} hint="integer micros" tone="muted" />
        <Stat label="Credits" value={int(totals.credits)} hint="integer micros" tone="muted" />
        <Stat
          label="Drift"
          value={int(totals.delta)}
          hint={balanced ? "debits == credits" : "debits != credits"}
          tone={balanced ? "bull" : "bear"}
        />
      </StatGrid>

      <Banner tone={balanced ? "bull" : "bear"} icon={balanced ? "check" : "warning"}>
        {balanced
          ? "DOUBLE-ENTRY INVARIANT HOLDS · sum(debits) == sum(credits) to the last micro-unit"
          : `LEDGER DRIFT · delta ${int(totals.delta)} micros (${usd(totals.delta)}) · reconcile before any new order`}
      </Banner>

      <Card
        title="Double-Entry Journal"
        subtitle="Two-phase outbox · DEBIT and CREDIT are the same integer amount, opposite accounts"
        icon="ledger"
        dense
        actions={
          <Badge tone="muted" mono>
            {int(journal.length)} ROWS
          </Badge>
        }
      >
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> LOADING JOURNAL…
          </div>
        ) : journal.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO JOURNAL ROWS"
              hint="The IBOR opens at zero. Fund a PAPER silo to post the first transfer."
            />
          </div>
        ) : (
          <Table dense columns={["TX ID", "WHEN", "SILO", "VAULT", "ACCOUNT", "DEBIT", "CREDIT", "MEMO"]}>
            {journal.map((row, index) => {
              const debit = accountParts(row.debit_account);
              const credit = accountParts(row.credit_account);
              const amount = numberAt(row, ["amount_micros"], 0);
              const code = text(row.code, "");
              return (
                <Row key={`${text(row.transfer_id, "tx")}-${index}`}>
                  <Cell mono tone="muted">
                    #{int(row.transfer_id)}
                  </Cell>
                  <Cell mono className="whitespace-nowrap">
                    {clockNs(row.created_ns)}
                  </Cell>
                  <Cell mono>{text(row.silo, debit.silo || "—")}</Cell>
                  <Cell>{debit.code || "—"}</Cell>
                  <Cell mono className="text-[10px]">
                    {`${debit.code} → ${credit.code}`}
                  </Cell>
                  <Cell align="right" tone="bear">
                    {int(amount)}
                  </Cell>
                  <Cell align="right" tone="bull">
                    {int(amount)}
                  </Cell>
                  <Cell tone="muted" className="text-[10px]">
                    {text(row.memo, TRANSFER_CODES[code] ?? `CODE ${code}`)}
                    <span className="ml-1 text-ink-3">{text(row.phase, "")}</span>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <Card
        title="Silo × Vault Custody"
        subtitle={`Integer micro-units · TAX_ESCROW reads the ${TAX_ESCROW_ACCOUNT} account`}
        icon="database"
        dense
      >
        <Table
          dense
          columns={["SILO", ...VAULTS.map((vault) => vault.toUpperCase()), "TAX_ESCROW", "SILO NAV"]}
        >
          {SILOS.map((silo) => {
            const vaults = asRecord(asRecord(silos[silo.id]).vaults);
            const escrow = safeNumber(balances[`${silo.id}.${TAX_ESCROW_ACCOUNT}`], 0);
            return (
              <Row key={silo.id}>
                <Cell>
                  <div className="font-semibold text-ink">{silo.short}</div>
                  <div className="font-mono text-[9.5px] text-ink-3">{silo.id}</div>
                </Cell>
                {VAULTS.map((vault) => {
                  const micros = numberAt(asRecord(vaults[vault]), ["micros"], 0);
                  return (
                    <Cell key={vault} align="right">
                      <div className="tabular-nums text-ink">{int(micros)}</div>
                      <div className="text-[9.5px] tabular-nums text-ink-3">{usd(micros)}</div>
                    </Cell>
                  );
                })}
                <Cell align="right" tone={escrow < 0 ? "bear" : "accent"}>
                  <div className="tabular-nums">{int(escrow)}</div>
                  <div className="text-[9.5px] tabular-nums opacity-70">{usd(escrow)}</div>
                </Cell>
                <Cell align="right" tone="muted">
                  <div className="tabular-nums">{int(numberAt(vaults, ["nav_micros"], 0))}</div>
                  <div className="text-[9.5px] tabular-nums opacity-70">
                    {usd(numberAt(vaults, ["nav_micros"], 0))}
                  </div>
                </Cell>
              </Row>
            );
          })}
        </Table>
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="Performance & Underwater Drawdown"
          subtitle="Settled logbook curve replayed backwards from live NAV"
          icon="activity"
        >
          <EquityDrawdownCurve equity={equity} drawdown={drawdown} height={200} />
          <KeyValue
            className="mt-3"
            columns={2}
            items={[
              {
                label: "Total Return",
                value: pct(performance.total_return_pct),
                tone: toneForNumber(performance.total_return_pct),
              },
              { label: "Max Drawdown", value: pct(performance.max_drawdown_pct), tone: "bear" },
              { label: "Sharpe", value: num(performance.sharpe, 2) },
              { label: "Sortino", value: num(performance.sortino, 2) },
              { label: "Calmar", value: num(performance.calmar, 2) },
              { label: "Profit Factor", value: num(performance.profit_factor, 2) },
              { label: "Trades", value: int(performance.trades) },
              { label: "Win Rate", value: pct(performance.win_rate_pct) },
            ]}
          />
        </Card>

        <Card
          title="Silo Attribution"
          subtitle="REALIZED / UNREALIZED / FEES / TAX in micros · NET from settled logbook"
          icon="sigma"
          dense
        >
          <Table dense columns={["SILO", "REALIZED", "UNREALIZED", "FEES", "TAX", "NET"]}>
            {SILOS.map((silo) => {
              const riskRow = asRecord(risk[silo.id]);
              const attributionRow = attribution.find((row) => text(row.key, "") === silo.id);
              const realized = numberAt(riskRow, ["realized_pnl_micros"], 0);
              const unrealized = numberAt(riskRow, ["unrealized_pnl_micros"], 0);
              const tax = numberAt(riskRow, ["total_tax_paid_micros"], 0);
              const fees = journal
                .filter((row) => text(row.silo, "") === silo.id && text(row.code, "") === "4")
                .reduce((sum, row) => sum + numberAt(row, ["amount_micros"], 0), 0);
              const net = attributionRow
                ? numberAt(attributionRow, ["net_pnl_micros"], 0)
                : realized + unrealized - tax;
              return (
                <Row key={silo.id}>
                  <Cell>{silo.short}</Cell>
                  <Cell align="right" tone={realized === 0 ? "muted" : realized > 0 ? "bull" : "bear"}>
                    {int(realized)}
                  </Cell>
                  <Cell align="right" tone={unrealized === 0 ? "muted" : unrealized > 0 ? "bull" : "bear"}>
                    {int(unrealized)}
                  </Cell>
                  <Cell align="right" tone={fees === 0 ? "muted" : "bear"}>
                    {int(fees)}
                  </Cell>
                  <Cell align="right" tone={tax === 0 ? "muted" : "bear"}>
                    {int(tax)}
                  </Cell>
                  <Cell align="right" tone={net === 0 ? "muted" : net > 0 ? "bull" : "bear"}>
                    {int(net)}
                  </Cell>
                </Row>
              );
            })}
          </Table>
        </Card>
      </div>

      <Card
        title="Finance Act 2026 · Segment-Aware Statutory Cost"
        subtitle="Itemised from the Ring 0 cost engine · integer micros per 1,000,000 micro notional"
        icon="shield"
        dense
      >
        <Table
          dense
          columns={["SILO", "FLOOR", "STT / SEC", "EXCHANGE", "GST", "STAMP", "VENUE FEE", "SLIP", "TOTAL"]}
        >
          {SILOS.map((silo) => {
            const entry = asRecord(statutory[silo.id]);
            const breakdown = asRecord(entry.breakdown);
            const components = rowsOf(breakdown.components, ["components"]);
            const total = numberAt(breakdown, ["total_micros"], 0);
            const hasBreakdown = components.length > 0;
            return (
              <Row key={silo.id}>
                <Cell>
                  <div className="font-semibold text-ink">{silo.short}</div>
                  <div className="text-[9.5px] text-ink-3">{silo.currency}</div>
                </Cell>
                <Cell align="right" mono>
                  {num(numberAt(entry, ["cost_floor_bps"], silo.floorBps), 2)}
                </Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, TAX_LEGS)) : "—"}</Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, EXCHANGE_LEGS)) : "—"}</Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, GST_LEGS)) : "—"}</Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, STAMP_LEGS)) : "—"}</Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, VENUE_LEGS)) : "—"}</Cell>
                <Cell align="right">{hasBreakdown ? int(legMicros(components, SLIPPAGE_LEGS)) : "—"}</Cell>
                <Cell align="right" tone="accent">
                  {hasBreakdown ? int(total) : "—"}
                </Cell>
              </Row>
            );
          })}
        </Table>
      </Card>

      {mode === "PAPER" ? (
        <Card
          title="Paper Capital Deposit"
          subtitle="PAPER only · LIVE capital never moves from this surface"
          icon="lock"
        >
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Silo" className="w-56">
              <SelectInput value={fundSilo} onChange={setFundSilo} options={SILO_OPTIONS} />
            </Field>
            <Field label="Amount (units)" className="w-44" hint="> 0 · floored to integer micros by Ring 0">
              <TextInput
                value={fundAmount}
                onChange={(event) => setFundAmount(event.target.value)}
                inputMode="decimal"
                mono
              />
            </Field>
            <Button variant="primary" icon="plus" onClick={() => void deposit()} disabled={busy !== null}>
              {busy === "deposit" ? "FUNDING…" : "Fund Paper"}
            </Button>
            <div className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              vault = ALL_CANONICAL · 35% compounder / 15% reserve ratchet applies on close
            </div>
          </div>
        </Card>
      ) : null}

      <Card
        title="4-Silo Reconciler"
        subtitle="LiveReconciler sweep · drift > 100 ppm freezes the silo, never the ledger"
        icon="shield"
        dense
      >
        <Table dense columns={["SILO", "DRIFT (BPS)", "THRESHOLD", "SYMBOLS", "RESULT", ""]}>
          {SILOS.map((silo) => {
            const row = reconciled[silo.id];
            const drift = row ? numberAt(row, ["drift_ppm"], 0) : 0;
            const threshold = row ? numberAt(row, ["threshold_ppm"], 100) : 100;
            const frozen = row ? row.frozen === true || row.breached === true : false;
            return (
              <Row key={`${silo.id}-reconcile`} tone={frozen ? "bear" : undefined}>
                <Cell>
                  <div className="font-semibold text-ink">{silo.short}</div>
                  <div className="font-mono text-[9.5px] text-ink-3">{silo.id}</div>
                </Cell>
                <Cell align="right">{row ? num(drift / 100, 4) : "—"}</Cell>
                <Cell align="right" tone="muted">
                  {row ? `${num(threshold / 100, 2)} bps` : "—"}
                </Cell>
                <Cell align="right" tone="muted">
                  {row ? int(numberAt(row, ["symbols_checked"], 0)) : "—"}
                </Cell>
                <Cell align="center">
                  {row ? (
                    <Badge tone={frozen ? "bear" : "bull"}>{frozen ? "FREEZE" : "BALANCED"}</Badge>
                  ) : (
                    <Badge tone="muted">NOT RUN</Badge>
                  )}
                </Cell>
                <Cell align="right">
                  <Button
                    size="sm"
                    variant="subtle"
                    icon="refresh"
                    disabled={busy !== null}
                    onClick={() => void reconcile(silo.id)}
                  >
                    {busy === silo.id ? "SWEEPING" : "Reconcile"}
                  </Button>
                </Cell>
              </Row>
            );
          })}
        </Table>
      </Card>
    </div>
  );
}

export default LedgerPage;
