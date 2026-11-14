/**
 * IBOR double-entry ledger.
 *
 * Four silos by four vaults in 128-bit micro-units, the two-phase transfer ring,
 * the ratchet preview and the conservation invariant. Reconciliation is the one
 * mutating action here and it never moves capital.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { bool, isRecord, money, num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";

export function LedgerPage(): ReactNode {
  const page = useApi(() => api.ledger(), []);
  const [busy, setBusy] = useState(false);
  const silos = isRecord(page.data?.silos) ? (page.data?.silos as Record<string, unknown>) : {};
  const siloIds = Object.keys(silos);
  const transfers = rows(page.data, "transfers");
  const balanced = bool(page.data, "double_entry.balanced", true);
  const debits = num(page.data, "double_entry.debits_micros", 0);
  const credits = num(page.data, "double_entry.credits_micros", 0);
  const navMicros = num(page.data, "counts.nav_micros", 0);

  const vaultRows = siloIds.flatMap((silo) => {
    const node = isRecord(silos[silo]) ? (silos[silo] as Record<string, unknown>) : {};
    const vaults = isRecord(node.vaults) ? (node.vaults as Record<string, unknown>) : {};
    return Object.entries(vaults).map(([vault, value]) => {
      const record = isRecord(value) ? value : {};
      return {
        silo,
        vault,
        balance: num(record, "balance_micros", num(record, "nav_micros", 0)),
        asset: str(record, "asset", str(record, "currency", "-")),
      };
    });
  });

  const reconcile = async () => {
    setBusy(true);
    try {
      const receipt = await api.reconcile();
      toast(`Reconciled: ${String(receipt.drift_bps ?? "0.00")} bps drift`, "success");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "reconcile rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 0 - IBOR"
        title="Double-entry ledger and tax floors"
        sub="Money is a 128-bit integer. Every transfer is staged in TigerBeetle and reconciled against PostgreSQL every sixty seconds."
        actions={
          <>
            <StatusBadge tone={balanced ? "bull" : "bear"} dot>
              {balanced ? "CONSERVED" : "DRIFT"}
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={reconcile} disabled={busy}>
              {busy ? "Reconciling..." : "Reconcile now"}
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Total NAV" value={money(navMicros)} sub="all silos, all vaults" />
        <MetricTile label="Debits" value={money(debits)} sub="micro-unit integers" />
        <MetricTile label="Credits" value={money(credits)} sub="must equal debits" tone={balanced ? "bull" : "bear"} />
        <MetricTile
          label="Transfers"
          value={String(transfers.length)}
          sub="last sixty, append-only"
          badge={<StatusBadge tone="muted">2-PHASE</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel title="Silo x vault balances" subtitle="Sixteen vaults, no cross-border wire" flush>
          <DataGrid
            columns={[
              {
                key: "silo",
                label: "Silo",
                render: (row: (typeof vaultRows)[number]) => <span className="nx-strong">{row.silo}</span>,
              },
              {
                key: "vault",
                label: "Vault",
                render: (row: (typeof vaultRows)[number]) => <span className="nx-dim">{row.vault}</span>,
              },
              {
                key: "asset",
                label: "Asset",
                render: (row: (typeof vaultRows)[number]) => (
                  <span className="nx-xs nx-muted">{row.asset}</span>
                ),
              },
              {
                key: "balance",
                label: "Balance",
                align: "right",
                render: (row: (typeof vaultRows)[number]) => (
                  <span className={row.balance > 0 ? "nx-bull" : "nx-muted"}>{money(row.balance)}</span>
                ),
              },
            ]}
            rows={vaultRows}
            getKey={(row) => `${row.silo}-${row.vault}`}
            empty={
              <EmptyCleanSlate
                title="Sixteen empty vaults"
                body="A clean slate opens every vault at zero and books no opening balance."
                hint="debits == credits == 0"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Conservation invariant" subtitle="Checked on every read">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Debits</span>
              <span className="nx-sm nx-strong">{money(debits)}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Credits</span>
              <span className="nx-sm nx-strong">{money(credits)}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Drift</span>
              <StatusBadge tone={balanced ? "bull" : "bear"}>
                {balanced ? "0.000%" : "BREACH"}
              </StatusBadge>
            </div>
            <div className="nx-hr" />
            <span className="nx-xs nx-muted">
              Drift above 0.01% freezes the silo and requires an operator release.
            </span>
          </TerminalPanel>

          <TerminalPanel title="Ratchet preview" subtitle="35% compounder, 15% reserve">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Long-term sweep</span>
              <span className="nx-sm nx-strong">{num(page.data, "ratchet.long_term_pct", 35)}%</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Reserve yield</span>
              <span className="nx-sm nx-strong">{num(page.data, "ratchet.reserve_pct", 15)}%</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Cross-border</span>
              <span className="nx-sm nx-strong">{num(page.data, "ratchet.cross_border_wire_bps", 0)} bps</span>
            </div>
            <div className="nx-hr" />
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Invariant</span>
              <span className="nx-xs nx-strong">{str(page.data, "invariant.status", "UNREPORTED").toUpperCase()}</span>
            </div>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Transfer ring" subtitle="Every row is a settled two-phase transfer" flush>
        <DataGrid
          columns={[
            {
              key: "id",
              label: "Transfer",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono">{str(row, "transfer_id", str(row, "id", "-")).slice(0, 12)}</span>
              ),
            },
            {
              key: "debit",
              label: "Debit account",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-dim">{str(row, "debit_account", str(row, "from", "-"))}</span>
              ),
            },
            {
              key: "credit",
              label: "Credit account",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-dim">{str(row, "credit_account", str(row, "to", "-"))}</span>
              ),
            },
            {
              key: "amount",
              label: "Amount",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num">{money(num(row, "amount_micros", 0))}</span>
              ),
            },
            {
              key: "state",
              label: "State",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const state = str(row, "status", str(row, "state", "POSTED")).toUpperCase();
                return <StatusBadge tone={state === "POSTED" || state === "SETTLED" ? "bull" : "accent"}>{state}</StatusBadge>;
              },
            },
          ]}
          rows={transfers}
          getKey={(row, index) => str(row, "transfer_id", String(index))}
          empty={
            <EmptyCleanSlate
              title="No transfer booked"
              body="The ledger has not moved a single micro-unit in this workspace."
              hint="append-only"
            />
          }
        />
      </TerminalPanel>
    </div>
  );
}
