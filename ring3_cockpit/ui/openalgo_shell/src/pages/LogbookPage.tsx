/**
 * Structured trade logbook (`/logbook`, alias `/journal`).
 *
 * One record form, one attribution readout and the chronological execution
 * journal. Gross P&L always loses its statutory cost drag before any net figure
 * is shown, so a sub-cost strategy can never look profitable in the journal.
 */

import { useState, type ReactNode } from "react";
import { ArrowRight, Eraser } from "lucide-react";
import {
  ConfirmDangerModal,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabularValue,
  pillTone,
} from "@/design-system/primitives";
import { api } from "@/lib/api";
import { int, text, toneOfLabel, usd, utc } from "@/lib/format";
import { useLocation } from "@/lib/router";
import { useAction, useResource } from "@/lib/use-api";
import type { JsonRecord, LogbookEntryRecord, LogbookPageData } from "@/lib/types";

const SILOS = ["INDIA_ZERODHA_DHAN", "CRYPTO_BINANCE", "US_IBKR", "FX_MT5"] as const;

interface EntryReceipt {
  ok: boolean;
  entry: LogbookEntryRecord;
  aggregate: LogbookPageData["aggregate"];
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function JournalRow({ row }: { row: LogbookEntryRecord }): ReactNode {
  return (
    <tr>
      <td className="num">
        <TabularValue value={row.sequence} kind="int" />
      </td>
      <td className="num">
        <span className="mono tnum text-[10.5px] text-[var(--dim)]">{utc(row.recorded_at_ns)}</span>
      </td>
      <td>
        <span className="mono text-[10px] text-[var(--muted)]">{row.silo}</span>
      </td>
      <td>
        <span className="mono text-[10.5px] text-[var(--ink)]">{row.symbol}</span>
      </td>
      <td>
        <StatusPill value={row.side} tone={row.side === "LONG" ? "ok" : "bad"} dot={false} />
      </td>
      <td>
        <span className="mono text-[10.5px] text-[var(--muted)]" title={row.setup_strategy}>
          {text(row.setup_strategy, "—")}
        </span>
      </td>
      <td className="num">
        <span className="mono tnum text-[10.5px] whitespace-nowrap">
          <TabularValue value={row.entry_price} kind="price" />
          <span className="px-1 text-[var(--dim)]" aria-hidden="true">
            →
          </span>
          <TabularValue value={row.exit_price} kind="price" />
        </span>
      </td>
      <td className="num">
        <TabularValue value={row.quantity} digits={2} />
      </td>
      <td className="num">
        <TabularValue value={row.gross_pnl} kind="usd" signTone />
      </td>
      <td className="num">
        <span className="mono tnum text-[var(--neg)]">
          −<TabularValue value={row.cost_drag} kind="usd" />
        </span>
      </td>
      <td className="num">
        <TabularValue value={row.net_pnl} kind="usd" signTone />
      </td>
      <td className="num">
        <TabularValue value={row.net_bps} kind="bps" signTone />
      </td>
      <td className="num">
        <TabularValue value={row.r_multiple} digits={2} signTone />
      </td>
      <td>
        <StatusPill value={row.outcome} tone={toneOfLabel(row.outcome)} />
      </td>
      <td>
        <StatusPill
          value={row.workspace}
          tone={row.workspace === "LIVE" ? "bad" : "accent"}
          dot={false}
        />
      </td>
      <td className="max-w-[240px]">
        <p className="truncate text-[11px] text-[var(--ink-soft)]" title={text(row.thesis, "")}>
          {text(row.thesis, "—")}
        </p>
        <p className="truncate text-[10.5px] text-[var(--dim)]" title={text(row.lessons_learned, "")}>
          {text(row.lessons_learned, "—")}
        </p>
      </td>
    </tr>
  );
}

export function LogbookPage(): ReactNode {
  const { workspace } = useLocation();
  const logbook = useResource("logbook", () => api.logbook(), 20_000);
  const recording = useAction<EntryReceipt>();
  const clearing = useAction<JsonRecord>();

  const [silo, setSilo] = useState<string>(SILOS[0]);
  const [symbol, setSymbol] = useState("NIFTY");
  const [side, setSide] = useState<"LONG" | "SHORT">("LONG");
  const [setupStrategy, setSetupStrategy] = useState("orb_breakout");
  const [entryPrice, setEntryPrice] = useState("24250");
  const [exitPrice, setExitPrice] = useState("24310");
  const [quantity, setQuantity] = useState("50");
  const [stopLoss, setStopLoss] = useState("24200");
  const [thesis, setThesis] = useState("");
  const [lessons, setLessons] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [clearOpen, setClearOpen] = useState(false);

  const data = logbook.data;
  const entryReceipt = recording.receipt;

  const recordTrade = async () => {
    const entry = numberOrNull(entryPrice);
    const exit = numberOrNull(exitPrice);
    const qty = numberOrNull(quantity);
    const stop = numberOrNull(stopLoss);
    if (symbol.trim() === "") {
      setFormError("Enter the traded symbol.");
      return;
    }
    if (entry === null || exit === null || entry <= 0 || exit <= 0) {
      setFormError("Entry and exit must both be positive numbers.");
      return;
    }
    if (qty === null || qty <= 0) {
      setFormError("Quantity must be a positive number.");
      return;
    }
    if (stopLoss.trim() !== "" && (stop === null || stop < 0)) {
      setFormError("Stop loss must be a non-negative number, or left empty.");
      return;
    }
    setFormError(null);
    const receipt = await recording.run(() =>
      api.logbookEntry({
        silo,
        symbol: symbol.trim().toUpperCase(),
        side,
        setup_strategy: setupStrategy.trim(),
        entry_price: entry,
        exit_price: exit,
        quantity: qty,
        stop_loss: stop ?? 0,
        thesis: thesis.trim(),
        lessons_learned: lessons.trim(),
        workspace: workspace === "paper" ? "PAPER" : "LIVE",
      }),
    );
    if (receipt !== null) {
      logbook.reload();
      setThesis("");
      setLessons("");
    }
  };

  const clearJournal = async () => {
    const receipt = await clearing.run(() => api.clearLogbook());
    if (receipt !== null) {
      setClearOpen(false);
      logbook.reload();
    }
  };

  if (!data) {
    return (
      <>
        <SectionHeader
          title="Structured Trade Logbook"
          eyebrow="ring3_cockpit · tax-net attribution"
          subtitle="Gross → cost drag → net P&L attribution, R-multiples and the chronological execution journal."
          actions={<StatusPill value={workspace === "live" ? "LIVE VAULT" : "SANDBOX"} tone="accent" />}
        />
        <div className="flex flex-col gap-[14px]">
          <GlassPanel title="Execution journal" eyebrow="ring0_vault.logbook">
            {logbook.loading ? (
              <LoadingBlock rows={5} label="Reading the execution journal" />
            ) : logbook.error ? (
              <ErrorBlock message={logbook.error} onRetry={logbook.reload} />
            ) : (
              <EmptyCleanSlate
                title="No journal snapshot published"
                body="Recorded trades, the tax-net aggregate and the statutory cost floors arrive in one read model."
                action={
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={logbook.reload}
                    aria-label="Refresh the execution journal"
                  >
                    Refresh journal
                  </button>
                }
              />
            )}
          </GlassPanel>
        </div>
      </>
    );
  }

  const aggregate = data.aggregate;
  const counts = data.counts;
  const appliedFloor = numberOrNull(data.cost_floor_bps[silo]);

  return (
    <>
      <SectionHeader
        title="Structured Trade Logbook"
        eyebrow="ring3_cockpit · tax-net attribution"
        subtitle="Every round trip is recorded with its statutory cost drag already deducted, so the net figure in the journal is the number the silo actually kept."
        actions={
          <div className="row gap-1.5">
            <StatusPill
              value={workspace === "live" ? "LIVE VAULT" : workspace === "paper" ? "PAPER SANDBOX" : "GATEWAY"}
              tone={workspace === "live" ? "bad" : "accent"}
            />
            <StatusPill
              value={`${int(counts.entries)} entries`}
              tone={counts.entries > 0 ? "ok" : "neutral"}
              dot={false}
            />
          </div>
        }
      />

      <div className="flex flex-col gap-[14px]">
        {logbook.error ? <ErrorBlock message={logbook.error} onRetry={logbook.reload} /> : null}

        <div className="split-2">
          <GlassPanel
            title="Record trade"
            eyebrow="paper or live · per workspace"
            actions={
              <StatusPill
                value={workspace === "paper" ? "PAPER RECORD" : "LIVE RECORD"}
                tone={workspace === "paper" ? "accent" : "bad"}
                dot={false}
              />
            }
          >
            <form
              className="grid gap-2 sm:grid-cols-2"
              onSubmit={(event) => {
                event.preventDefault();
                void recordTrade();
              }}
              aria-label="Record a structured trade"
            >
              <label className="field">
                <span className="field-label">Silo</span>
                <select
                  className="select"
                  value={silo}
                  onChange={(event) => setSilo(event.target.value)}
                  aria-label="Trade silo"
                >
                  {SILOS.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Symbol</span>
                <input
                  className="input"
                  value={symbol}
                  onChange={(event) => setSymbol(event.target.value)}
                  aria-label="Traded symbol"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Side</span>
                <select
                  className="select"
                  value={side}
                  onChange={(event) => setSide(event.target.value === "SHORT" ? "SHORT" : "LONG")}
                  aria-label="Trade side"
                >
                  <option value="LONG">LONG</option>
                  <option value="SHORT">SHORT</option>
                </select>
              </label>
              <label className="field">
                <span className="field-label">Setup / strategy</span>
                <input
                  className="input"
                  value={setupStrategy}
                  onChange={(event) => setSetupStrategy(event.target.value)}
                  aria-label="Setup or strategy label"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Entry</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={entryPrice}
                  onChange={(event) => setEntryPrice(event.target.value)}
                  aria-label="Entry price"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Exit</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={exitPrice}
                  onChange={(event) => setExitPrice(event.target.value)}
                  aria-label="Exit price"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Quantity</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={quantity}
                  onChange={(event) => setQuantity(event.target.value)}
                  aria-label="Traded quantity"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Stop loss</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={stopLoss}
                  onChange={(event) => setStopLoss(event.target.value)}
                  aria-label="Stop loss price"
                  autoComplete="off"
                />
              </label>
              <label className="field sm:col-span-2">
                <span className="field-label">Thesis</span>
                <textarea
                  className="textarea"
                  value={thesis}
                  onChange={(event) => setThesis(event.target.value)}
                  aria-label="Trade thesis"
                  rows={3}
                />
              </label>
              <label className="field sm:col-span-2">
                <span className="field-label">Lessons learned</span>
                <textarea
                  className="textarea"
                  value={lessons}
                  onChange={(event) => setLessons(event.target.value)}
                  aria-label="Lessons learned"
                  rows={2}
                />
              </label>
              <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
                <button
                  type="submit"
                  className="btn btn-accent"
                  disabled={recording.busy}
                  aria-label="Record the trade in the logbook"
                >
                  {recording.busy ? "Recording…" : "Record trade"}
                </button>
                <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
                  applied floor{" "}
                  <TabularValue value={appliedFloor} digits={2} /> bps
                </span>
              </div>
              {formError ? (
                <p role="alert" className="mono text-[10.5px] text-[var(--neg)] sm:col-span-2">
                  {formError}
                </p>
              ) : null}
            </form>

            {recording.error ? (
              <div className="mt-2">
                <ErrorBlock message={recording.error} onRetry={() => void recordTrade()} />
              </div>
            ) : null}

            {entryReceipt ? (
              <div className="mt-3 rounded-[var(--r-sm)] border border-[var(--line)] bg-[rgba(4,7,13,0.6)] px-3 py-2">
                <div className="row-between flex-wrap gap-2">
                  <div className="row gap-2">
                    <StatusPill
                      value={`#${int(entryReceipt.entry.sequence)} · ${entryReceipt.entry.outcome}`}
                      tone={toneOfLabel(entryReceipt.entry.outcome)}
                    />
                    <span className="mono text-[10px] text-[var(--dim)]">
                      {entryReceipt.entry.silo} · {entryReceipt.entry.symbol} ·{" "}
                      {entryReceipt.entry.workspace}
                    </span>
                  </div>
                  <span className="mono tnum text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
                    receipt
                  </span>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  <div>
                    <p className="eyebrow">net p&amp;l</p>
                    <p className="mono tnum text-[13px] text-[var(--ink)]">
                      <TabularValue value={entryReceipt.entry.net_pnl} kind="usd" signTone />
                    </p>
                  </div>
                  <div>
                    <p className="eyebrow">r multiple</p>
                    <p className="mono tnum text-[13px] text-[var(--ink)]">
                      <TabularValue value={entryReceipt.entry.r_multiple} digits={2} signTone />
                    </p>
                  </div>
                  <div>
                    <p className="eyebrow">cost drag</p>
                    <p className="mono tnum text-[13px] text-[var(--neg)]">
                      −<TabularValue value={entryReceipt.entry.cost_drag} kind="usd" />
                      <span className="ml-1 text-[9.5px] text-[var(--dim)]">
                        <TabularValue value={entryReceipt.entry.cost_bps} digits={2} /> bps
                      </span>
                    </p>
                  </div>
                </div>
              </div>
            ) : null}
          </GlassPanel>

          <GlassPanel
            title="P&L attribution"
            eyebrow={`${int(counts.wins)} wins · ${int(counts.losses)} losses · ${int(counts.flat)} flat`}
          >
            <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-1">
              <div className="tile">
                <p className="kpi-label">gross p&amp;l</p>
                <p className="mono tnum mt-0.5 text-[15px] font-semibold text-[var(--ink)]">
                  {usd(aggregate.gross_pnl)}
                </p>
                <p className="kpi-hint">before statutory drag</p>
              </div>
              <ArrowRight size={13} className="text-[var(--dim)]" aria-hidden="true" />
              <div className="tile">
                <p className="kpi-label">cost drag</p>
                <p className="mono tnum mt-0.5 text-[15px] font-semibold text-[var(--neg)]">
                  −{usd(Math.abs(aggregate.cost_drag))}
                </p>
                <p className="kpi-hint">tax + fee floor</p>
              </div>
              <ArrowRight size={13} className="text-[var(--dim)]" aria-hidden="true" />
              <div className="tile">
                <p className="kpi-label">net p&amp;l</p>
                <p
                  className={`mono tnum mt-0.5 text-[15px] font-semibold ${
                    aggregate.net_pnl >= 0 ? "text-[var(--pos)]" : "text-[var(--neg)]"
                  }`}
                >
                  {usd(aggregate.net_pnl)}
                </p>
                <p className="kpi-hint">after the tax-net floor</p>
              </div>
            </div>

            <div className="mt-2.5 grid grid-cols-2 gap-2">
              <MetricTile
                label="Win rate"
                value={
                  <>
                    <TabularValue value={aggregate.win_rate * 100} digits={1} />%
                  </>
                }
                hint={
                  <span>
                    <TabularValue value={counts.wins} kind="int" />W /{" "}
                    <TabularValue value={counts.losses} kind="int" />L
                  </span>
                }
                tone={aggregate.win_rate >= 0.5 ? "pos" : "warn"}
              />
              <MetricTile
                label="Average R"
                value={<TabularValue value={aggregate.avg_r_multiple} signTone />}
                hint="per recorded trade"
                tone={pillTone(aggregate.avg_r_multiple)}
              />
              <MetricTile
                label="Total R"
                value={<TabularValue value={aggregate.total_r_multiple} signTone />}
                hint={
                  <span>
                    <TabularValue value={counts.entries} kind="int" /> entries
                  </span>
                }
                tone={pillTone(aggregate.total_r_multiple)}
              />
              <MetricTile
                label="Average cost"
                value={
                  <>
                    <TabularValue value={aggregate.avg_cost_bps} digits={2} /> bps
                  </>
                }
                hint="statutory drag per trade"
                tone="dim"
              />
            </div>

            <p className="mono mt-2.5 border-t border-[var(--line)] pt-2 text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
              sec_014 — gross minus statutory drag equals net, always
            </p>
          </GlassPanel>
        </div>

        <GlassPanel
          title="Execution journal"
          eyebrow="chronological · newest last"
          actions={
            <div className="row gap-1.5">
              <StatusPill value={`${int(counts.paper)} paper`} tone="accent" dot={false} />
              <StatusPill value={`${int(counts.live)} live`} tone="bad" dot={false} />
              <button
                type="button"
                className="btn btn-sm btn-ghost text-[var(--neg)]"
                onClick={() => setClearOpen(true)}
                aria-label="Clear the execution journal"
              >
                <Eraser size={12} aria-hidden="true" />
                Clear Logbook
              </button>
            </div>
          }
        >
          {clearing.error ? (
            <div className="mb-2">
              <ErrorBlock message={clearing.error} onRetry={() => void clearJournal()} />
            </div>
          ) : null}

          {data.entries.length === 0 ? (
            <EmptyCleanSlate
              title={data.clean_slate ? "Clean slate — no trades recorded" : "No journal entries"}
              body="Record a round trip above. The journal stores the gross P&L, the statutory cost drag and the net the silo actually kept, in that order."
            />
          ) : (
            <div className="scroll-x">
              <table className="table table-compact">
                <caption className="sr-only">
                  Chronological execution journal with gross P&L, cost drag, net P&L, net basis points,
                  R-multiple, outcome and workspace per trade.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">Recorded (UTC)</th>
                    <th scope="col">Silo</th>
                    <th scope="col">Symbol</th>
                    <th scope="col">Side</th>
                    <th scope="col">Setup</th>
                    <th scope="col">Entry → exit</th>
                    <th scope="col">Qty</th>
                    <th scope="col">Gross</th>
                    <th scope="col">Cost drag</th>
                    <th scope="col">Net</th>
                    <th scope="col">Net bps</th>
                    <th scope="col">R</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Workspace</th>
                    <th scope="col">Thesis / lessons</th>
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((entryRecord) => (
                    <JournalRow key={entryRecord.entry_id} row={entryRecord} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </GlassPanel>
      </div>

      <ConfirmDangerModal
        open={clearOpen}
        title="Clear the execution journal"
        body={
          <span>
            Every recorded trade is removed and the aggregate resets to zero. The IBOR ledger is untouched —
            this only clears the journal. Type CLEAR to confirm.
          </span>
        }
        requireText="CLEAR"
        confirmLabel="Clear Logbook"
        busy={clearing.busy}
        onCancel={() => setClearOpen(false)}
        onConfirm={() => void clearJournal()}
      />
    </>
  );
}
