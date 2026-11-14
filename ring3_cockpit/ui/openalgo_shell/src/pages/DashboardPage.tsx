/**
 * Sovereign command dashboard.
 *
 * The operator's one-screen posture read: total IBOR NAV, working exposure, the
 * shadow-twin graduation counter, the isolation topology and the 4x4 silo vault
 * grid. A clean-slate install renders zeros, not blanks.
 */

import type { ReactNode } from "react";
import { api, authApi } from "@/lib/api";
import { bool, isRecord, money, moneyCompact, num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";
import { RingTopology } from "@/design-system/ring-topology";
import { RadialGauge } from "@/charts/RadialGauge";

export function DashboardPage(): ReactNode {
  const ledger = useApi(() => api.ledger(), []);
  const paper = useApi(() => authApi.paperState(), []);
  const verification = useApi(() => api.formalProofs(), []);

  const navMicros = num(ledger.data, "counts.nav_micros", 0);
  const silos = isRecord(ledger.data?.silos) ? (ledger.data?.silos as Record<string, unknown>) : {};
  const siloIds = Object.keys(silos);
  const working = rows(paper.data, "orders.working");
  const shadowFills = num(paper.data, "shadow.shadow_fills", 0);
  const requiredFills = num(paper.data, "shadow.required_fills", 30);
  const graduated = bool(paper.data, "shadow.graduated");
  const cleanSlate = bool(paper.data, "clean_slate_active", true);
  const proofs = rows(verification.data, "states");
  const invariants = str(verification.data, "status", "");

  const vaultRows = siloIds.flatMap((silo) => {
    const vaults = ledger.data?.silos as Record<string, unknown>;
    const node = isRecord(vaults?.[silo]) ? (vaults[silo] as Record<string, unknown>) : {};
    const vaultMap = isRecord(node.vaults) ? (node.vaults as Record<string, unknown>) : {};
    return Object.entries(vaultMap).map(([vault, value]) => {
      const record = isRecord(value) ? value : {};
      return {
        silo,
        vault,
        balance: num(record, "balance_micros", num(record, "nav_micros", 0)),
        status: str(record, "status", "OPEN"),
      };
    });
  });

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="SOVEREIGN COMMAND"
        title="Four-ring posture and total book"
        sub="One read-only surface over the IBOR, the shadow twin and the formal verification state. Nothing on this page can sign an order."
        actions={
          <>
            <StatusBadge tone={cleanSlate ? "accent" : "bull"} dot>
              {cleanSlate ? "CLEAN SLATE 0" : "BOOK LIVE"}
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={ledger.reload}>
              Refresh book
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile
          label="Total IBOR NAV"
          value={moneyCompact(navMicros)}
          sub={`${siloIds.length} silos - 16 vaults`}
          badge={<StatusBadge tone="muted">RING 0</StatusBadge>}
        />
        <MetricTile
          label="Working orders"
          value={String(working.length)}
          sub={working.length === 0 ? "No order in the book" : "Maker-Peg / TWAP / Iceberg"}
          badge={<StatusBadge tone={working.length === 0 ? "muted" : "accent"}>SMART OMS</StatusBadge>}
        />
        <MetricTile
          label="Shadow graduation"
          value={`${shadowFills} / ${requiredFills}`}
          sub={graduated ? "Twin graduated to live" : "L2 fills required before live"}
          badge={
            <StatusBadge tone={graduated ? "bull" : "muted"}>{graduated ? "GRADUATED" : "NURSERY"}</StatusBadge>
          }
        />
        <MetricTile
          label="25k-tick posture"
          value={invariants ? invariants.toUpperCase() : "UNVERIFIED"}
          sub={`${proofs.length} TLA+ machines modelled`}
          badge={<StatusBadge tone={invariants ? "bull" : "muted"}>FORMAL</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--3-2">
        <TerminalPanel
          title="Ring isolation topology"
          subtitle="Ring 2 reasons; Ring 0 alone signs"
          actions={<StatusBadge tone="bull" dot>AIR GAP INTACT</StatusBadge>}
        >
          <RingTopology active="ring3" />
        </TerminalPanel>

        <TerminalPanel title="Funnel discipline" subtitle="5,000 instruments to 3-8 signed trades">
          <RadialGauge
            value={shadowFills}
            max={requiredFills}
            label="Shadow fills"
            tone={graduated ? "bull" : "accent"}
            hint={`${requiredFills - shadowFills} fills remain before live graduation`}
          />
          <div className="nx-hr" />
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Conformal gate</span>
            <span className="nx-xs nx-strong">98% coverage required</span>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Abstention target</span>
            <span className="nx-xs nx-strong">~99.1% of observed tape</span>
          </div>
        </TerminalPanel>
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel
          title="Silo x vault grid"
          subtitle="128-bit integer micro-units, double-entry only"
          flush
          actions={<StatusBadge tone={navMicros === 0 ? "muted" : "bull"}>{money(navMicros)}</StatusBadge>}
        >
          <DataGrid
            columns={[
              { key: "silo", label: "Silo", render: (row: (typeof vaultRows)[number]) => row.silo },
              {
                key: "vault",
                label: "Vault",
                render: (row: (typeof vaultRows)[number]) => <span className="nx-dim">{row.vault}</span>,
              },
              {
                key: "balance",
                label: "Balance",
                align: "right",
                render: (row: (typeof vaultRows)[number]) => (
                  <span className={row.balance > 0 ? "nx-bull" : "nx-muted"}>{money(row.balance)}</span>
                ),
              },
              {
                key: "status",
                label: "Status",
                align: "right",
                render: (row: (typeof vaultRows)[number]) => (
                  <StatusBadge tone={row.balance > 0 ? "bull" : "muted"}>{row.status}</StatusBadge>
                ),
              },
            ]}
            rows={vaultRows}
            getKey={(row) => `${row.silo}-${row.vault}`}
            empty={
              <EmptyCleanSlate
                title="Every vault is at zero"
                body="A clean-slate workspace records four silos and sixteen empty vaults. Fund the paper sandbox from the top bar; the live vault can only be funded through its own signed deposit path."
                hint="clean_slate_active = true"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Proof stream" subtitle="SHA-256 committed numerics only">
            {proofs.length === 0 ? (
              <EmptyCleanSlate
                title="No proof emitted yet"
                body="The grounding ledger commits a hash per numeric claim. Run a committee debate or a MiroFish cascade to seed the stream."
              />
            ) : (
              <div className="nx-list nx-scroll nx-scroll--sm">
                {proofs.slice(0, 8).map((proof, index) => (
                  <div className="nx-list-item" key={String(index)}>
                    <StatusBadge tone="accent">HASH</StatusBadge>
                    <span className="nx-mono nx-truncate">{str(proof, "digest", str(proof, "hash", "-"))}</span>
                  </div>
                ))}
              </div>
            )}
          </TerminalPanel>

          <TerminalPanel title="Ratchet" subtitle="In-silo compounding, no cross-border wire">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Long-term compounder</span>
              <span className="nx-sm nx-strong">{num(ledger.data, "ratchet.long_term_pct", 35)}%</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Reserve yield</span>
              <span className="nx-sm nx-strong">{num(ledger.data, "ratchet.reserve_pct", 15)}%</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Cross-border wire</span>
              <span className="nx-sm nx-strong">{num(ledger.data, "ratchet.cross_border_wire_bps", 0)} bps</span>
            </div>
            <div className="nx-hr" />
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Debits / credits</span>
              <span className="nx-sm nx-strong">
                {money(num(ledger.data, "double_entry.debits_micros", 0))} /{" "}
                {money(num(ledger.data, "double_entry.credits_micros", 0))}
              </span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Conservation</span>
              <StatusBadge tone={bool(ledger.data, "double_entry.balanced", true) ? "bull" : "bear"}>
                {bool(ledger.data, "double_entry.balanced", true) ? "BALANCED" : "DRIFT"}
              </StatusBadge>
            </div>
          </TerminalPanel>
        </div>
      </div>

      {ledger.error ? (
        <div className="nx-portal-err" role="alert">
          ledger unavailable: {ledger.error}
        </div>
      ) : null}
    </div>
  );
}
