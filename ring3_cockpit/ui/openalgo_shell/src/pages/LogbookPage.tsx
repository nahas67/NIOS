/**
 * Structured trade logbook.
 *
 * Gross, cost drag and net are separated so a strategy can never be judged on
 * a number that ignored spread, fee or tax. R-multiples are computed from the
 * planned risk, not from the realised loss.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { bool, money, num, rows, str, titleCase } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  ConfirmModal,
  DataGrid,
  EmptyCleanSlate,
  Field,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";

export function LogbookPage(): ReactNode {
  const page = useApi(() => api.logbook(), []);
  const [open, setOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [symbol, setSymbol] = useState("RELIANCE");
  const [side, setSide] = useState("LONG");
  const [quantity, setQuantity] = useState("100");
  const [entry, setEntry] = useState("2900");
  const [exit, setExit] = useState("2935");

  const entries = rows(page.data, "entries");
  const aggregate = page.data?.aggregate;
  const gross = num(aggregate, "gross_pnl", 0);
  const cost = num(aggregate, "cost_drag", 0);
  const net = num(aggregate, "net_pnl", 0);
  const wins = num(aggregate, "wins", 0);
  const losses = num(aggregate, "losses", 0);
  const winRate = wins + losses > 0 ? (wins / (wins + losses)) * 100 : 0;
  const cleanSlate = bool(page.data, "clean_slate", entries.length === 0);

  const add = async () => {
    setBusy(true);
    try {
      await api.logbookEntry({
        symbol: symbol.toUpperCase(),
        side,
        quantity: Number(quantity) || 0,
        entry_price: Number(entry) || 0,
        exit_price: Number(exit) || 0,
        strategy_id: "manual-operator",
        notes: "operator-entered execution record",
      });
      toast("Execution record appended", "success");
      setOpen(false);
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "entry rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    try {
      await api.clearLogbook();
      toast("Logbook cleared", "success");
      setClearOpen(false);
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "clear rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 3 - AUDIT"
        title="Trade logbook and execution journal"
        sub="Gross to cost drag to net. A win rate without the tax floor is a vanity metric, so the floor sits beside every aggregate."
        actions={
          <>
            <StatusBadge tone={cleanSlate ? "muted" : "accent"}>
              {cleanSlate ? "NO ENTRIES" : `${entries.length} RECORDS`}
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh journal
            </Btn>
            <Btn size="sm" tone="primary" onClick={() => setOpen(true)}>
              Append record
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Gross P&L" value={money(gross * 1_000_000)} sub="before costs" tone={gross > 0 ? "bull" : "neutral"} />
        <MetricTile label="Cost drag" value={money(cost * 1_000_000)} sub="spread + fee + tax" tone={cost > 0 ? "bear" : "neutral"} />
        <MetricTile
          label="Net P&L"
          value={money(net * 1_000_000)}
          sub={`floor ${num(page.data, "cost_floor_bps", 0).toFixed(1)} bps`}
          tone={net > 0 ? "bull" : "bear"}
        />
        <MetricTile
          label="Win rate"
          value={`${winRate.toFixed(1)}%`}
          sub={`${wins}W / ${losses}L`}
          badge={<StatusBadge tone="muted">R-MULTIPLE</StatusBadge>}
        />
      </div>

      <TerminalPanel
        title="Execution journal"
        subtitle="Chronological and append-only"
        flush
        actions={
          <Btn size="sm" tone="danger" onClick={() => setClearOpen(true)} disabled={entries.length === 0}>
            Clear journal
          </Btn>
        }
      >
        <DataGrid
          columns={[
            {
              key: "ts",
              label: "Time",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono nx-muted">{str(row, "ts", str(row, "created_at", "-")).slice(0, 19)}</span>
              ),
            },
            {
              key: "symbol",
              label: "Instrument",
              render: (row: Record<string, unknown>) => (
                <span className="nx-strong">{str(row, "symbol", "-")}</span>
              ),
            },
            {
              key: "side",
              label: "Side",
              render: (row: Record<string, unknown>) => {
                const value = str(row, "side", "LONG").toUpperCase();
                return <StatusBadge tone={value === "LONG" ? "bull" : "bear"}>{value}</StatusBadge>;
              },
            },
            {
              key: "qty",
              label: "Qty",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num">{num(row, "quantity", num(row, "qty", 0))}</span>
              ),
            },
            {
              key: "gross",
              label: "Gross",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num nx-dim">{num(row, "gross_pnl", 0).toFixed(2)}</span>
              ),
            },
            {
              key: "cost",
              label: "Cost",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num nx-bear">-{num(row, "cost_drag", 0).toFixed(2)}</span>
              ),
            },
            {
              key: "net",
              label: "Net",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const value = num(row, "net_pnl", 0);
                return <span className={value >= 0 ? "nx-num nx-bull" : "nx-num nx-bear"}>{value.toFixed(2)}</span>;
              },
            },
            {
              key: "r",
              label: "R",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const value = num(row, "r_multiple", 0);
                return <span className={value >= 0 ? "nx-num" : "nx-num nx-bear"}>{value.toFixed(2)}R</span>;
              },
            },
            {
              key: "outcome",
              label: "Outcome",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const value = str(row, "outcome", "FLAT").toUpperCase();
                return (
                  <StatusBadge tone={value === "WIN" ? "bull" : value === "LOSS" ? "bear" : "muted"}>
                    {value}
                  </StatusBadge>
                );
              },
            },
          ]}
          rows={entries}
          getKey={(row, index) => str(row, "id", `entry-${index}`)}
          empty={
            <EmptyCleanSlate
              title="Journal empty"
              body="No trade has been recorded in this workspace. A clean slate writes zero rows, not a placeholder row."
              hint="clean_slate = true"
            />
          }
        />
      </TerminalPanel>

      <div className="nx-grid nx-grid--2">
        <TerminalPanel title="Attribution" subtitle="Where the gross edge goes">
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Gross</span>
            <span className="nx-sm nx-strong">{money(gross * 1_000_000)}</span>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Cost drag</span>
            <span className="nx-sm nx-bear nx-strong">-{money(cost * 1_000_000)}</span>
          </div>
          <div className="nx-hr" />
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Net</span>
            <span className={net >= 0 ? "nx-sm nx-bull nx-strong" : "nx-sm nx-bear nx-strong"}>
              {money(net * 1_000_000)}
            </span>
          </div>
          <span className="nx-xs nx-muted">
            India STT alone is a 5.5 bps floor before a single fill is booked.
          </span>
        </TerminalPanel>

        <TerminalPanel title="Journal composition" subtitle="Every row is attributable">
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Wins</span>
            <StatusBadge tone="bull">{wins}</StatusBadge>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Losses</span>
            <StatusBadge tone="bear">{losses}</StatusBadge>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Flats</span>
            <StatusBadge tone="muted">{num(aggregate, "flats", 0)}</StatusBadge>
          </div>
          <div className="nx-hr" />
          <span className="nx-xs nx-muted">
            {entries.length > 0
              ? `Latest record: ${titleCase(str(entries[0], "outcome", "FLAT"))} on ${str(entries[0], "symbol", "-")}.`
              : "No record to summarise."}
          </span>
        </TerminalPanel>
      </div>

      <ConfirmModal
        open={open}
        title="Append an execution record"
        description="A manual record is an audit entry, not an order. It never reaches the Smart OMS or the broker."
        confirmLabel="Append record"
        busy={busy}
        onConfirm={add}
        onClose={() => setOpen(false)}
      >
        <div className="nx-grid nx-grid--2">
          <Field label="Symbol">
            <input className="nx-input" value={symbol} onChange={(event) => setSymbol(event.target.value)} />
          </Field>
          <Field label="Side">
            <select className="nx-select" value={side} onChange={(event) => setSide(event.target.value)}>
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
            </select>
          </Field>
          <Field label="Quantity">
            <input className="nx-input" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
          </Field>
          <Field label="Entry price">
            <input className="nx-input" value={entry} onChange={(event) => setEntry(event.target.value)} />
          </Field>
          <Field label="Exit price">
            <input className="nx-input" value={exit} onChange={(event) => setExit(event.target.value)} />
          </Field>
        </div>
      </ConfirmModal>

      <ConfirmModal
        open={clearOpen}
        title="Clear the journal"
        description="This deletes every execution record in the workspace. The IBOR and the live vault are untouched."
        confirmLabel="Clear journal"
        tone="danger"
        busy={busy}
        onConfirm={clear}
        onClose={() => setClearOpen(false)}
      >
        <p className="nx-portal-note">{entries.length} record(s) will be removed.</p>
      </ConfirmModal>
    </div>
  );
}
