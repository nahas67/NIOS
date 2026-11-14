/**
 * IBOR double-entry ledger (`/ledger`).
 *
 * Conservation first: debits and credits are compared in micro-units, the four
 * silos × four vaults grid is read from the ledger's own balances, and the
 * ratchet / tax floors are shown as the statutory contract rather than a local
 * estimate. Paper capital can only be funded from the non-live workspaces.
 */

import { useState, type ReactNode } from "react";
import { Scale } from "lucide-react";
import {
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  SectionHeader,
  StatusPill,
  TabularValue,
  passTone,
} from "@/design-system/primitives";
import { api, authApi } from "@/lib/api";
import { bool, clamp, int, money, num, pct, record, strings, text, titleize, utc } from "@/lib/format";
import { useLocation } from "@/lib/router";
import { useAction, useResource } from "@/lib/use-api";
import type { JsonRecord, LedgerPageData, LedgerReconcileReceipt } from "@/lib/types";

const SILOS = ["INDIA_ZERODHA_DHAN", "CRYPTO_BINANCE", "US_IBKR", "FX_MT5"] as const;

const VAULTS = [
  { name: "ReserveYield", label: "Reserve Yield", tone: "pos" },
  { name: "LongTermCompounder", label: "Long-Term Compounder", tone: "accent" },
  { name: "TacticalSwing", label: "Tactical Swing", tone: "warn" },
  { name: "SpinalScalper", label: "Spinal Scalper", tone: "neg" },
] as const;

const FALLBACK_TAX_FLOORS: Array<{ silo: string; statutory: number; floor: number }> = [
  { silo: "INDIA_ZERODHA_DHAN", statutory: 5.5, floor: 8.5 },
  { silo: "CRYPTO_BINANCE", statutory: 16.0, floor: 16.0 },
  { silo: "US_IBKR", statutory: 2.8, floor: 2.8 },
  { silo: "FX_MT5", statutory: 1.6, floor: 1.6 },
];

interface TaxFloorRow {
  silo: string;
  statutory: number | null;
  floor: number | null;
  source: "ledger" | "statutory schedule";
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function booleanOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function normaliseKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/** Vault balances arrive as `{micros, nav}` objects or as bare micro integers. */
function microsOf(value: unknown): number | null {
  const direct = numberOrNull(value);
  if (direct !== null) return direct;
  return numberOrNull(record(value).micros);
}

function vaultMicros(vaults: Record<string, unknown>, index: number, name: string): number | null {
  const target = normaliseKey(name);
  for (const [key, value] of Object.entries(vaults)) {
    if (normaliseKey(key) === target) return microsOf(value);
  }
  const byIndex = vaults[String(index)];
  return byIndex === undefined ? null : microsOf(byIndex);
}

function reconcileRows(receipt: LedgerReconcileReceipt): Array<Record<string, unknown>> {
  if (Array.isArray(receipt.silos)) {
    const list: unknown[] = receipt.silos;
    return list.filter((row): row is Record<string, unknown> => row !== null && typeof row === "object");
  }
  return Object.entries(record(receipt.silos)).map(([silo, value]) => ({ silo, ...record(value) }));
}

function taxFloorRows(data: LedgerPageData): TaxFloorRow[] {
  const rows: TaxFloorRow[] = [];
  for (const [silo, raw] of Object.entries(data.silos)) {
    const siloRecord = record(raw);
    const costFloor = record(siloRecord.cost_floor);
    const statutory =
      numberOrNull(siloRecord.statutory_tax_bps) ?? numberOrNull(costFloor.statutory_tax_bps);
    const spread = numberOrNull(costFloor.spread_bps);
    const makerFee = numberOrNull(costFloor.maker_fee_bps);
    const composedFloor =
      spread !== null && makerFee !== null && statutory !== null
        ? spread + 2 * makerFee + statutory
        : null;
    const floor =
      numberOrNull(siloRecord.cost_floor_bps) ??
      numberOrNull(costFloor.round_trip_cost_bps) ??
      composedFloor;
    const schedule = FALLBACK_TAX_FLOORS.find((row) => row.silo === silo) ?? null;
    rows.push({
      silo,
      statutory: statutory ?? schedule?.statutory ?? null,
      floor: floor ?? schedule?.floor ?? null,
      source: statutory !== null || floor !== null ? "ledger" : "statutory schedule",
    });
  }
  if (rows.length > 0) return rows;
  return FALLBACK_TAX_FLOORS.map((row) => ({
    silo: row.silo,
    statutory: row.statutory,
    floor: row.floor,
    source: "statutory schedule" as const,
  }));
}

function scalarLabel(value: unknown): string {
  if (typeof value === "boolean") return value ? "YES" : "NO";
  if (typeof value === "number") return num(value, 3);
  return text(value);
}

export function LedgerPage(): ReactNode {
  const { workspace } = useLocation();
  const ledger = useResource("ledger", () => api.ledger(), 15_000);
  const reconcile = useAction<LedgerReconcileReceipt>();
  const funding = useAction<JsonRecord>();

  const [depositSilo, setDepositSilo] = useState<string>(SILOS[0]);
  const [depositAmount, setDepositAmount] = useState("100000");
  const [depositError, setDepositError] = useState<string | null>(null);

  const data = ledger.data;

  const runReconcile = async () => {
    const receipt = await reconcile.run(() => api.reconcile());
    if (receipt !== null) ledger.reload();
  };

  const fundPaper = async () => {
    const amount = numberOrNull(depositAmount);
    if (amount === null || amount <= 0) {
      setDepositError("Enter a positive amount in account units.");
      return;
    }
    setDepositError(null);
    const receipt = await funding.run(() =>
      authApi.fundPaper({ silo: depositSilo, amount_units: amount }),
    );
    if (receipt !== null) ledger.reload();
  };

  if (!data) {
    return (
      <>
        <SectionHeader
          title="IBOR Double-Entry Ledger"
          eyebrow="ring0_vault · 128-bit micro-units · append-only"
          subtitle="Debit/credit conservation, the 4×4 vault grid, the in-silo ratchet and the statutory tax floors."
          actions={<StatusPill value={workspace === "live" ? "LIVE VAULT" : "SANDBOX"} tone="accent" />}
        />
        <div className="flex flex-col gap-[14px]">
          <GlassPanel title="Ledger conservation" eyebrow="ring0_vault.ibor">
            {ledger.loading ? (
              <LoadingBlock rows={5} label="Reading the double-entry ledger" />
            ) : ledger.error ? (
              <ErrorBlock message={ledger.error} onRetry={ledger.reload} />
            ) : (
              <EmptyCleanSlate
                title="No ledger snapshot published"
                body="The IBOR read model carries the vault balances, double-entry totals and tax floors in one frame."
                action={
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={ledger.reload}
                    aria-label="Refresh the ledger snapshot"
                  >
                    Refresh ledger
                  </button>
                }
              />
            )}
          </GlassPanel>
        </div>
      </>
    );
  }

  const debits = numberOrNull(data.double_entry.debits_micros) ?? 0;
  const credits = numberOrNull(data.double_entry.credits_micros) ?? 0;
  const drift = Math.abs(debits - credits);
  const balanced = data.double_entry.balanced === true && drift === 0;
  const invariant = record(data.invariant);
  const violations = Array.isArray(invariant.violations) ? invariant.violations.length : 0;
  const ratchet = data.ratchet;
  const longTermPct = clamp(numberOrNull(ratchet.long_term_pct) ?? 0, 0, 100);
  const reservePct = clamp(numberOrNull(ratchet.reserve_pct) ?? 0, 0, 100);
  const siloPct = Math.max(0, 100 - longTermPct - reservePct);
  const wireBps = numberOrNull(ratchet.cross_border_wire_bps) ?? 0;
  const taxRows = taxFloorRows(data);
  const reconcileReceipt = reconcile.receipt;
  const reconcileRowData = reconcileReceipt ? reconcileRows(reconcileReceipt) : [];
  const transfers = data.transfers.slice(-6).reverse();
  const backendEntries = Object.entries(record(data.backends));
  const fundingReceipt = funding.receipt;
  const totalNavMicros = numberOrNull(data.counts.nav_micros);

  return (
    <>
      <SectionHeader
        title="IBOR Double-Entry Ledger"
        eyebrow="ring0_vault · 128-bit micro-units · append-only"
        subtitle="Conservation is proven before it is displayed: debits and credits are summed in micro-units, and a drift above 0.01% freezes the silo rather than editing the book."
        actions={
          <div className="row gap-1.5">
            <StatusPill
              value={workspace === "live" ? "LIVE VAULT" : workspace === "paper" ? "PAPER SANDBOX" : "GATEWAY"}
              tone={workspace === "live" ? "bad" : "accent"}
            />
            <StatusPill value={`${int(data.counts.silos)} silos`} tone="neutral" dot={false} />
          </div>
        }
      />

      <div className="flex flex-col gap-[14px]">
        {ledger.error ? <ErrorBlock message={ledger.error} onRetry={ledger.reload} /> : null}

        <GlassPanel
          title="Conservation"
          eyebrow="sum(debits) == sum(credits) · micro-units"
          actions={
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void runReconcile()}
              disabled={reconcile.busy}
              aria-label="Run the four-silo reconciler sweep"
            >
              <Scale size={12} aria-hidden="true" />
              {reconcile.busy ? "Reconciling…" : "Run 4-Silo Reconciler Sweep"}
            </button>
          }
        >
          <div
            className="tile tile-quiet flex flex-wrap items-center justify-between gap-4"
          >
            <div className="flex flex-wrap items-center gap-4">
              <div>
                <p className="kpi-label">sum debits</p>
                <p className="mono tnum text-[18px] font-semibold text-[var(--ink)]">{money(debits)}</p>
              </div>
              <span className="text-[var(--dim)]" aria-hidden="true">
                =
              </span>
              <div>
                <p className="kpi-label">sum credits</p>
                <p className="mono tnum text-[18px] font-semibold text-[var(--ink)]">{money(credits)}</p>
              </div>
              <StatusPill value={balanced ? "BALANCED" : "DRIFT DETECTED"} tone={balanced ? "ok" : "bad"} />
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <div>
                <p className="kpi-label">absolute drift</p>
                <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                  <TabularValue value={drift} kind="int" /> micros
                </p>
              </div>
              <div>
                <p className="kpi-label">invariant</p>
                <div className="mt-0.5 row gap-1.5">
                  <StatusPill
                    value={bool(invariant.holds) ? "HOLDS" : "VIOLATION"}
                    tone={bool(invariant.holds) ? "ok" : "bad"}
                  />
                  <StatusPill
                    value={`${int(violations)} overdrafts`}
                    tone={violations === 0 ? "neutral" : "bad"}
                    dot={false}
                  />
                  <StatusPill
                    value={`${int(invariant.accounts)} accounts`}
                    tone="neutral"
                    dot={false}
                  />
                </div>
              </div>
            </div>
          </div>

          {reconcile.error ? (
            <div className="mt-3">
              <ErrorBlock message={reconcile.error} onRetry={() => void runReconcile()} />
            </div>
          ) : null}

          {reconcileReceipt ? (
            <div className="mt-3">
              <div className="row-between flex-wrap gap-2">
                <div className="row gap-2">
                  <StatusPill
                    value={text(reconcileReceipt.status, "RECONCILED")}
                    tone={reconcileReceipt.status === "DRIFT_BREACH" ? "bad" : "ok"}
                  />
                  <StatusPill
                    value={
                      bool(reconcileReceipt.all_within_threshold)
                        ? "ALL WITHIN 0.01%"
                        : "DRIFT THRESHOLD BREACHED"
                    }
                    tone={bool(reconcileReceipt.all_within_threshold) ? "ok" : "bad"}
                  />
                </div>
                <span className="mono tnum text-[10px] tracking-[0.08em] text-[var(--dim)] uppercase">
                  <TabularValue value={reconcileReceipt.silo_count} kind="int" /> silos checked
                </span>
              </div>
              <div className="scroll-x mt-2">
                <table className="table table-compact">
                  <caption className="sr-only">
                    Reconciler sweep per silo: drift in parts per million against the 0.0100% freeze threshold,
                    symbols checked, and any repair action.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Silo</th>
                      <th scope="col">Drift (ppm)</th>
                      <th scope="col">Within threshold</th>
                      <th scope="col">Frozen</th>
                      <th scope="col">Symbols</th>
                      <th scope="col">Repair action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reconcileRowData.map((row) => {
                      const within = booleanOrNull(row.within_threshold);
                      const frozen = booleanOrNull(row.frozen);
                      return (
                        <tr key={text(row.silo, "—")}>
                          <td>
                            <span className="mono text-[10.5px] text-[var(--ink-soft)]">
                              {text(row.silo, "—")}
                            </span>
                          </td>
                          <td className="num">
                            <TabularValue value={row.drift_ppm} digits={2} />
                          </td>
                          <td>
                            <StatusPill
                              value={within === null ? "UNREPORTED" : within ? "WITHIN" : "BREACH"}
                              tone={passTone(within)}
                            />
                          </td>
                          <td>
                            <StatusPill
                              value={frozen === null ? "UNREPORTED" : frozen ? "FROZEN" : "OPEN"}
                              tone={frozen === null ? "neutral" : frozen ? "bad" : "ok"}
                            />
                          </td>
                          <td className="num">
                            <TabularValue value={row.symbols_checked} kind="int" />
                          </td>
                          <td>
                            <span className="mono text-[10.5px] text-[var(--dim)]">
                              {text(row.repair_action, "none required")}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <div className="mt-3">
            <div className="row-between">
              <p className="eyebrow">recent two-phase transfers</p>
              <span className="mono tnum text-[10px] text-[var(--dim)]">
                <TabularValue value={data.counts.transfers} kind="int" /> total · showing{" "}
                <TabularValue value={transfers.length} kind="int" />
              </span>
            </div>
            {transfers.length === 0 ? (
              <p className="mono mt-1.5 text-[10.5px] text-[var(--faint)]">
                No transfer has been staged yet — the outbox is empty.
              </p>
            ) : (
              <div className="scroll-x mt-1.5">
                <table className="table table-compact">
                  <caption className="sr-only">Most recent two-phase outbox transfers.</caption>
                  <thead>
                    <tr>
                      <th scope="col">Transfer</th>
                      <th scope="col">Phase</th>
                      <th scope="col">Debit → credit</th>
                      <th scope="col">Amount</th>
                      <th scope="col">Created (UTC)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {transfers.map((transfer, index) => {
                      const phase = text(transfer.phase, "—");
                      return (
                        <tr key={`${text(transfer.transfer_id, "transfer")}-${index}`}>
                          <td className="num">
                            <TabularValue value={transfer.transfer_id} kind="int" />
                          </td>
                          <td>
                            <StatusPill
                              value={phase}
                              tone={phase === "POSTED" ? "ok" : phase === "VOIDED" ? "bad" : "warn"}
                            />
                          </td>
                          <td>
                            <span className="mono text-[10.5px] text-[var(--ink-soft)]">
                              {text(transfer.debit_account, "—")}
                              <span className="px-1 text-[var(--dim)]" aria-hidden="true">
                                →
                              </span>
                              {text(transfer.credit_account, "—")}
                            </span>
                          </td>
                          <td className="num">
                            <TabularValue value={transfer.amount_micros} kind="money" />
                          </td>
                          <td className="num">
                            <span className="mono tnum text-[10.5px]">{utc(transfer.created_ns)}</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </GlassPanel>

        <GlassPanel
          title="4 silos × 4 vaults"
          eyebrow="micro-unit balances · share of silo NAV"
          actions={
            <span className="mono tnum text-[10px] tracking-[0.08em] text-[var(--dim)] uppercase">
              total nav {money(totalNavMicros)}
            </span>
          }
        >
          <div className="grid-auto-300">
            {Object.entries(data.silos).map(([siloId, raw]) => {
              const silo = record(raw);
              const vaults = record(silo.vaults);
              const resolved = VAULTS.map((vault, index) => ({
                ...vault,
                micros: vaultMicros(vaults, index, vault.name),
              }));
              const summed = resolved.reduce((sum, vault) => sum + (vault.micros ?? 0), 0);
              const known = resolved.filter((vault) => vault.micros !== null).length;
              const total = numberOrNull(vaults.nav_micros) ?? (known > 0 ? summed : null);
              return (
                <article key={siloId} className="tile">
                  <header className="row-between gap-2">
                    <div className="min-w-0">
                      <p className="eyebrow">silo</p>
                      <p className="mono truncate text-[11.5px] font-medium text-[var(--ink)]">{siloId}</p>
                    </div>
                    <div className="row flex-none gap-1.5">
                      <StatusPill value={text(silo.mode, "SIMULATED")} tone="accent" />
                      {bool(silo.frozen) ? <StatusPill value="FROZEN" tone="bad" /> : null}
                    </div>
                  </header>

                  <div className="mt-2.5 flex flex-col gap-2">
                    {resolved.map((vault) => {
                      const share =
                        total !== null && total > 0 && vault.micros !== null
                          ? (vault.micros / total) * 100
                          : 0;
                      return (
                        <div
                          key={vault.name}
                          className="grid grid-cols-[132px_minmax(0,1fr)_88px] items-center gap-2"
                        >
                          <span className="truncate text-[10.5px] text-[var(--muted)]">{vault.label}</span>
                          <InlineBar
                            value={share}
                            max={100}
                            tone={vault.tone}
                            label={`${siloId} ${vault.label} share of NAV`}
                          />
                          <span className="mono tnum text-right text-[10.5px] text-[var(--ink-soft)]">
                            {money(vault.micros)}
                          </span>
                        </div>
                      );
                    })}
                  </div>

                  <footer className="mt-2.5 flex items-center justify-between border-t border-[var(--line)] pt-2">
                    <span className="eyebrow">total nav</span>
                    <span className="mono tnum text-[13px] font-semibold text-[var(--ink)]">
                      {money(total)}
                    </span>
                  </footer>
                </article>
              );
            })}
          </div>
        </GlassPanel>

        <div className="split-2">
          <GlassPanel title="In-silo ratchet" eyebrow="realised profit only · never cross-border">
            <div
              className="flex h-3.5 w-full overflow-hidden rounded-full border border-[var(--line)]"
              role="img"
              aria-label={`Ratchet split: compounder ${pct(longTermPct, 0)}, reserve ${pct(reservePct, 0)}, silo ${pct(siloPct, 0)}`}
            >
              <span className="h-full bg-[var(--accent)]" style={{ width: `${longTermPct}%` }} />
              <span className="h-full bg-[var(--accent-2)]" style={{ width: `${reservePct}%` }} />
              <span className="h-full bg-[var(--info)]" style={{ width: `${siloPct}%` }} />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2">
              <div>
                <p className="kpi-label">compounder</p>
                <p className="mono tnum text-[15px] text-[var(--accent)]">
                  <TabularValue value={longTermPct} kind="pct" digits={0} />
                </p>
              </div>
              <div>
                <p className="kpi-label">reserve</p>
                <p className="mono tnum text-[15px] text-[var(--accent-2)]">
                  <TabularValue value={reservePct} kind="pct" digits={0} />
                </p>
              </div>
              <div>
                <p className="kpi-label">silo</p>
                <p className="mono tnum text-[15px] text-[var(--info)]">
                  <TabularValue value={siloPct} kind="pct" digits={0} />
                </p>
              </div>
            </div>
            <div className="mt-3 border-t border-[var(--line)] pt-2">
              <div className="row-between flex-wrap gap-2">
                <p className="text-[11.5px] text-[var(--muted)]">
                  Zero cross-border wires · measured wire budget{" "}
                  <span className="mono tnum text-[var(--ink-soft)]">
                    <TabularValue value={wireBps} digits={2} /> bps
                  </span>
                </p>
                <StatusPill
                  value={wireBps > 0 ? "CROSS-BORDER EXPOSURE" : "NO CROSS-BORDER WIRES"}
                  tone={wireBps > 0 ? "bad" : "ok"}
                />
              </div>
              <p className="mono mt-1 text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
                sec_018 Law 5 — capital compounds inside its own silo
              </p>
            </div>
          </GlassPanel>

          <GlassPanel
            title={workspace === "live" ? "Paper capital deposit" : "Fund the paper sandbox"}
            eyebrow="simulated capital · zero real risk"
          >
            {workspace === "live" ? (
              <div className="tile">
                <p className="text-[12px] leading-relaxed text-[var(--muted)]">
                  Funding belongs to the paper sandbox. The live vault is capitalised through your broker
                  relationship — Ring 3 never moves money, and no deposit control exists on this surface.
                </p>
                <div className="mt-2">
                  <StatusPill value="LIVE · DEPOSITS DISABLED" tone="bad" />
                </div>
              </div>
            ) : (
              <form
                className="grid gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  void fundPaper();
                }}
                aria-label="Deposit simulated paper capital"
              >
                <label className="field">
                  <span className="field-label">Silo</span>
                  <select
                    className="select"
                    value={depositSilo}
                    onChange={(event) => setDepositSilo(event.target.value)}
                    aria-label="Paper capital target silo"
                  >
                    {SILOS.map((silo) => (
                      <option key={silo} value={silo}>
                        {silo}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Amount (account units)</span>
                  <input
                    className="input tnum"
                    inputMode="decimal"
                    value={depositAmount}
                    onChange={(event) => setDepositAmount(event.target.value)}
                    aria-label="Paper capital amount in account units"
                    autoComplete="off"
                  />
                </label>
                <button
                  type="submit"
                  className="btn btn-accent"
                  disabled={funding.busy}
                  aria-label="Deposit simulated capital into the selected silo"
                >
                  {funding.busy ? "Depositing…" : "Deposit paper capital"}
                </button>
                {depositError ? (
                  <p role="alert" className="mono text-[10.5px] text-[var(--neg)]">
                    {depositError}
                  </p>
                ) : null}
                <p className="mono text-[9.5px] leading-relaxed text-[var(--faint)]">
                  Credited to ReserveYield first; the ratchet only applies to realised profit.
                </p>
              </form>
            )}

            {funding.error ? (
              <div className="mt-2">
                <ErrorBlock message={funding.error} onRetry={() => void fundPaper()} />
              </div>
            ) : null}

            {fundingReceipt ? (
              <div className="mt-2 rounded-[var(--r-sm)] border border-[var(--line)] bg-[rgba(4,7,13,0.6)] px-3 py-2">
                <div className="row-between flex-wrap gap-2">
                  <StatusPill value={bool(fundingReceipt.ok) ? "CREDITED" : "REJECTED"} tone={bool(fundingReceipt.ok) ? "ok" : "bad"} />
                  <span className="mono tnum text-[10.5px] text-[var(--dim)]">
                    {text(fundingReceipt.silo, depositSilo)} ·{" "}
                    <TabularValue value={fundingReceipt.amount_units} digits={2} /> units
                  </span>
                </div>
                {fundingReceipt.clean_slate_active === false ? (
                  <p className="mono mt-1 text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
                    clean slate released
                  </p>
                ) : null}
              </div>
            ) : null}
          </GlassPanel>
        </div>

        <GlassPanel title="Statutory tax floors" eyebrow="round-trip cost floor before any net edge">
          <div className="scroll-x">
            <table className="table table-compact">
              <caption className="sr-only">
                Statutory tax and round-trip cost floor in basis points per silo, with the source of each row.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Silo</th>
                  <th scope="col">Statutory tax (bps)</th>
                  <th scope="col">Cost floor (bps)</th>
                  <th scope="col">Source</th>
                </tr>
              </thead>
              <tbody>
                {taxRows.map((row) => (
                  <tr key={row.silo}>
                    <td>
                      <span className="mono text-[10.5px] text-[var(--ink-soft)]">{row.silo}</span>
                    </td>
                    <td className="num">
                      <TabularValue value={row.statutory} digits={2} />
                    </td>
                    <td className="num">
                      <TabularValue value={row.floor} digits={2} />
                    </td>
                    <td>
                      <StatusPill
                        value={row.source}
                        tone={row.source === "ledger" ? "ok" : "neutral"}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mono mt-2 text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
            sec_014 — the tax-net floor is subtracted before a strategy is called profitable
          </p>
        </GlassPanel>

        <GlassPanel title="IBOR backends" eyebrow="TigerBeetle · PostgreSQL 16 · provisioning probes">
          {backendEntries.length === 0 ? (
            <EmptyCleanSlate
              title="No backend probe in this build"
              body="The provisioning probe did not publish a TigerBeetle / PostgreSQL status document. The in-process double-entry journal still enforces debits_must_not_exceed_credits."
            />
          ) : (
            <div className="grid gap-[14px] xl:grid-cols-2">
              {backendEntries.map(([key, value]) => {
                const detail = record(value);
                const readyRaw =
                  detail.ready ?? detail.valid ?? detail.available ?? detail.found ?? detail.ok;
                const ready = typeof readyRaw === "boolean" ? readyRaw : null;
                const schema = record(detail.schema);
                const tables = strings(schema.tables);
                const scalars = Object.entries(detail).filter(
                  ([, item]) =>
                    typeof item === "string" || typeof item === "number" || typeof item === "boolean",
                );
                const executionBlocked = detail.execution_blocked === true;
                return (
                  <article key={key} className="tile">
                    <header className="row-between gap-2">
                      <div className="min-w-0">
                        <p className="eyebrow">backend</p>
                        <p className="text-[12.5px] font-medium text-[var(--ink)]">{titleize(key)}</p>
                      </div>
                      <div className="row flex-none gap-1.5">
                        {executionBlocked ? <StatusPill value="EXECUTION BLOCKED" tone="bad" /> : null}
                        <StatusPill
                          value={ready === null ? "UNPROBED" : ready ? "READY" : "NOT READY"}
                          tone={passTone(ready)}
                        />
                      </div>
                    </header>

                    {scalars.length > 0 ? (
                      <dl className="mt-2.5 grid gap-1">
                        {scalars.slice(0, 7).map(([field, item]) => (
                          <div key={field} className="row-between gap-2 border-b border-[var(--line)] pb-1">
                            <dt className="eyebrow">{field.replace(/_/g, " ")}</dt>
                            <dd className="mono tnum text-right text-[10.5px] text-[var(--ink-soft)]">
                              {scalarLabel(item)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    ) : (
                      <p className="mono mt-2 text-[10.5px] text-[var(--faint)]">
                        Probe returned no scalar fields.
                      </p>
                    )}

                    {tables.length > 0 ? (
                      <div className="mt-2.5 border-t border-[var(--line)] pt-2">
                        <div className="row-between gap-2">
                          <span className="eyebrow">pg16 schema</span>
                          <StatusPill
                            value={`${int(tables.length)} tables`}
                            tone="info"
                            dot={false}
                          />
                        </div>
                        <div className="chip-row mt-1.5">
                          {tables.slice(0, 12).map((table) => (
                            <span key={table} className="pill">
                              {table}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}
        </GlassPanel>
      </div>
    </>
  );
}
