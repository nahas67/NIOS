/**
 * Brain and contagion X-ray.
 *
 * The ring process isolation matrix, the MiroFish five-archetype cascade and
 * the staged committee transcript. A cascade above 0.75 fragility revokes the
 * long leases before any deep thesis is allowed to continue.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { num, rows, str, titleCase } from "@/lib/format";
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
import { ContagionFlowGraph } from "@/charts/ContagionFlowGraph";
import { RadialGauge } from "@/charts/RadialGauge";

const ARCHETYPES = ["PANIC", "GREED", "HERD", "CONTRARIAN", "LIQUIDITY"];

function matrixFrom(source: unknown): number[][] {
  const ticks = rows(source, "cascade_matrix");
  if (ticks.length > 0) {
    return ARCHETYPES.map((_, rowIndex) => {
      const line = ticks[rowIndex];
      const values = Array.isArray(line?.values) ? (line.values as unknown[]) : [];
      return [0, 1, 2].map((col) => Number(values[col]) || 0);
    });
  }
  return ARCHETYPES.map(() => [0, 0, 0]);
}

export function BrainPage(): ReactNode {
  const page = useApi(() => api.brain(), []);
  const [shock, setShock] = useState<Record<string, unknown> | null>(null);
  const [debate, setDebate] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  const processes = rows(page.data, "processes");
  const leases = rows(page.data, "leases");
  const transcript = rows(debate, "transcript");
  const matrix = matrixFrom(shock);
  const fragility = num(shock, "cascade_fragility_index", num(shock, "fragility", 0));
  const verdict = str(debate, "verdict", str(debate, "decision", ""));

  const runShock = async () => {
    setBusy(true);
    try {
      const next = await api.mirofishShock({ symbol: "RELIANCE", shock_bps: -180, sentiment_tag: "PANIC" });
      setShock(next);
      toast("MiroFish three-tick cascade complete", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "cascade rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const runDebate = async () => {
    setBusy(true);
    try {
      const next = await api.committeeDebate({ silo: "INDIA", symbol: "RELIANCE" });
      setDebate(next);
      toast("Investment committee staged", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "debate rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - BICAMERAL BRAIN"
        title="Contagion X-ray and committee transcript"
        sub="Five archetypes propagate for three ticks before any deep thesis. A single-asset narrative is never treated as portfolio truth."
        actions={
          <>
            <StatusBadge tone={fragility >= 0.75 ? "bear" : "muted"}>
              FRAGILITY {fragility.toFixed(3)}
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Re-read rings
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Ring processes" value={String(processes.length)} sub="isolated supervisors" />
        <MetricTile label="Active leases" value={String(leases.length)} sub="short TTL only" />
        <MetricTile
          label="Cascade fragility"
          value={fragility.toFixed(3)}
          sub="0.75 revokes long leases"
          tone={fragility >= 0.75 ? "bear" : "bull"}
        />
        <MetricTile
          label="Committee"
          value={verdict ? titleCase(verdict) : "-"}
          sub="System 1 lease plus System 2 debate"
          badge={<StatusBadge tone="muted">STAGED</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--3-2">
        <TerminalPanel
          title="MiroFish cascade"
          subtitle="5 archetypes x 3 ticks, directed propagation"
          actions={
            <Btn size="sm" tone="primary" onClick={runShock} disabled={busy}>
              Run cascade
            </Btn>
          }
        >
          <ContagionFlowGraph matrix={matrix} fragility={fragility} />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Lease revocation" subtitle="Tripwire at 0.75">
            <RadialGauge
              value={fragility}
              max={1}
              label="Fragility"
              unit=""
              tone={fragility >= 0.75 ? "bear" : "bull"}
              hint={fragility >= 0.75 ? "Long leases revoked" : "Long leases intact"}
            />
          </TerminalPanel>
          <TerminalPanel title="Committee" subtitle="Bull, bear and three-way risk">
            <Btn tone="primary" block onClick={runDebate} disabled={busy}>
              {busy ? "Debating..." : "Stage investment committee"}
            </Btn>
            <p className="nx-xs nx-muted" style={{ marginTop: 6 }}>
              Four analysts, a bull and bear researcher, a trader and three risk debaters. Nine to fifteen
              seconds per staged run.
            </p>
          </TerminalPanel>
        </div>
      </div>

      <div className="nx-grid nx-grid--2">
        <TerminalPanel title="Ring process matrix" subtitle="Isolation is enforced by the supervisor" flush>
          <DataGrid
            columns={[
              {
                key: "ring",
                label: "Ring",
                render: (row: Record<string, unknown>) => (
                  <StatusBadge tone="muted">{str(row, "ring", str(row, "name", "RING"))}</StatusBadge>
                ),
              },
              {
                key: "pid",
                label: "PID",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-mono">{num(row, "pid", 0)}</span>
                ),
              },
              {
                key: "state",
                label: "State",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const state = str(row, "state", str(row, "status", "UNKNOWN")).toUpperCase();
                  return (
                    <StatusBadge tone={state === "RUNNING" || state === "UP" ? "bull" : state === "DOWN" ? "bear" : "muted"}>
                      {state}
                    </StatusBadge>
                  );
                },
              },
            ]}
            rows={processes}
            getKey={(row, index) => str(row, "ring", String(index))}
            empty={<EmptyCleanSlate title="Supervisor not reporting" body="No ring process snapshot is available." />}
          />
        </TerminalPanel>

        <TerminalPanel title="Committee transcript" subtitle="Every figure carries a commitment" flush>
          <DataGrid
            columns={[
              {
                key: "speaker",
                label: "Speaker",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong nx-xs">{titleCase(str(row, "speaker", str(row, "role", "ANALYST")))}</span>
                ),
              },
              {
                key: "stance",
                label: "Stance",
                render: (row: Record<string, unknown>) => {
                  const stance = str(row, "stance", str(row, "side", "NEUTRAL")).toUpperCase();
                  return (
                    <StatusBadge tone={stance.startsWith("B") ? "bull" : stance.startsWith("S") ? "bear" : "muted"}>
                      {stance}
                    </StatusBadge>
                  );
                },
              },
              {
                key: "text",
                label: "Argument",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-xs nx-dim nx-truncate" style={{ maxWidth: 320, display: "inline-block" }}>
                    {str(row, "text", str(row, "argument", str(row, "content", "-")))}
                  </span>
                ),
              },
            ]}
            rows={transcript}
            getKey={(row, index) => `${str(row, "speaker", "s")}-${index}`}
            empty={
              <EmptyCleanSlate
                title="No committee staged"
                body="Stage a debate to populate the transcript. Nothing is pre-written on a clean slate."
                hint="System 2 only runs on demand"
              />
            }
          />
        </TerminalPanel>
      </div>
    </div>
  );
}
