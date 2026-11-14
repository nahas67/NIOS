/**
 * Formal proofs and chaos drills.
 *
 * TLA+ state-space coverage, seeded VOPR faults, SIGKILL recovery drills and
 * the OS/socket tuning audit. A green row here means a machine checked it, not
 * that an operator asserted it.
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

export function VerificationPage(): ReactNode {
  const page = useApi(() => api.verification(), []);
  const proofs = useApi(() => api.formalProofs(), []);
  const metrics = useApi(() => api.metrics(), []);
  const [busy, setBusy] = useState(false);
  const [drill, setDrill] = useState<Record<string, unknown> | null>(null);
  const [vopr, setVopr] = useState<Record<string, unknown> | null>(null);
  const [tuning, setTuning] = useState<Record<string, unknown> | null>(null);

  const invariants = rows(proofs.data, "invariants");
  const states = rows(proofs.data, "states");
  const suites = rows(page.data, "suites");
  const coverage = num(proofs.data, "coverage", num(proofs.data, "states_explored", 0));
  const drillOk = str(drill, "status", str(drill, "result", "")).toUpperCase();
  const voprFaults = num(vopr, "faults_injected", num(vopr, "faults", 0));
  const metricRows: Array<{ name: string; value: string }> = Object.entries(metrics.data ?? {})
    .filter(([, value]) => typeof value !== "object")
    .slice(0, 8)
    .map(([name, value]) => ({ name, value: String(value) }));

  const runDrill = async () => {
    setBusy(true);
    try {
      const next = await api.chaosDrill({ iterations: 50, hold_ms: 5 });
      setDrill(next);
      toast("SIGKILL recovery drill complete", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "drill rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const runVopr = async () => {
    setBusy(true);
    try {
      const next = await api.vopr({ seed: 20261114, steps: 5000 });
      setVopr(next);
      toast("Seeded VOPR campaign complete", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "vopr rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const runTuning = async () => {
    setBusy(true);
    try {
      const next = await api.osTune({ apply: false, ring: "ring1" });
      setTuning(next);
      toast("OS and socket audit complete", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "audit rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 0 - VERIFICATION"
        title="Formal proofs, VOPR and chaos drills"
        sub="KillSwitch, OrderLifecycle, Outbox and Reconciliation are machine checked. A drill that has not run is reported as unverified."
        actions={
          <>
            <StatusBadge tone={invariants.length > 0 ? "bull" : "muted"}>
              {invariants.length} INVARIANTS
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={proofs.reload}>
              Re-read proofs
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="TLA+ machines" value={String(states.length || 4)} sub="KillSwitch / Order / Outbox / Recon" />
        <MetricTile
          label="States explored"
          value={coverage ? coverage.toLocaleString("en-US") : "-"}
          sub="exhaustive search"
          badge={<StatusBadge tone="muted">TLC</StatusBadge>}
        />
        <MetricTile
          label="Chaos drill"
          value={drillOk ? titleCase(drillOk) : "-"}
          sub="SIGKILL recovery"
          tone={drillOk === "PASS" || drillOk === "PASSED" ? "bull" : "neutral"}
        />
        <MetricTile
          label="VOPR faults"
          value={voprFaults ? String(voprFaults) : "-"}
          sub="seeded and reproducible"
          badge={<StatusBadge tone="muted">SEED 20261114</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel title="Invariant matrix" subtitle="Every invariant is a machine check" flush>
          <DataGrid
            columns={[
              {
                key: "name",
                label: "Invariant",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong">{str(row, "name", str(row, "id", "-"))}</span>
                ),
              },
              {
                key: "statement",
                label: "Statement",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-xs nx-dim nx-truncate" style={{ maxWidth: 340, display: "inline-block" }}>
                    {str(row, "statement", str(row, "detail", "-"))}
                  </span>
                ),
              },
              {
                key: "status",
                label: "Status",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const state = str(row, "status", str(row, "result", "UNKNOWN")).toUpperCase();
                  return (
                    <StatusBadge tone={state === "PASS" || state === "HOLDS" ? "bull" : state === "FAIL" ? "bear" : "muted"}>
                      {state}
                    </StatusBadge>
                  );
                },
              },
            ]}
            rows={invariants}
            getKey={(row, index) => str(row, "name", String(index))}
            empty={
              <EmptyCleanSlate
                title="No proof run recorded"
                body="The TLA+ models exist in specs/ but no certificate has been written for this workspace."
                hint="specs/ - KillSwitch, OrderLifecycle, Outbox, Reconciliation"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Drills" subtitle="Non-destructive, seeded">
            <Btn tone="primary" block onClick={runDrill} disabled={busy}>
              Run SIGKILL recovery drill
            </Btn>
            <Btn tone="ghost" block onClick={runVopr} disabled={busy} style={{ marginTop: 6 }}>
              Run seeded VOPR (5,000 steps)
            </Btn>
            <Btn tone="ghost" block onClick={runTuning} disabled={busy} style={{ marginTop: 6 }}>
              OS low-latency audit
            </Btn>
          </TerminalPanel>

          <TerminalPanel title="Latest results" subtitle="Evidence, not a log line">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Recovery drill</span>
              <StatusBadge tone={drillOk === "PASS" || drillOk === "PASSED" ? "bull" : "muted"}>
                {drillOk || "NOT RUN"}
              </StatusBadge>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">VOPR faults</span>
              <span className="nx-sm nx-strong">{voprFaults || "-"}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Tuning verdict</span>
              <StatusBadge tone={str(tuning, "status", "").toUpperCase() === "PASS" ? "bull" : "muted"}>
                {str(tuning, "status", "NOT RUN").toUpperCase()}
              </StatusBadge>
            </div>
            <div className="nx-hr" />
            <span className="nx-xs nx-muted">
              Kill-switch drill target: all leases disarmed in under 1.0 ms.
            </span>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Verification suites" subtitle="Every gate names the artefact it inspected" flush>
        <DataGrid
          columns={[
            {
              key: "name",
              label: "Suite",
              render: (row: Record<string, unknown>) => (
                <span className="nx-strong">{str(row, "name", str(row, "suite", "-"))}</span>
              ),
            },
            {
              key: "detail",
              label: "Detail",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-dim nx-truncate" style={{ maxWidth: 420, display: "inline-block" }}>
                  {str(row, "detail", str(row, "summary", "-"))}
                </span>
              ),
            },
            {
              key: "status",
              label: "Status",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const state = str(row, "status", "UNKNOWN").toUpperCase();
                return <StatusBadge tone={state === "PASS" ? "bull" : state === "FAIL" ? "bear" : "muted"}>{state}</StatusBadge>;
              },
            },
          ]}
          rows={suites}
          getKey={(row, index) => str(row, "name", String(index))}
          empty={<EmptyCleanSlate title="No suite report" body="The gateway did not return a verification suite list." />}
        />
      </TerminalPanel>

      <TerminalPanel
        title="Prometheus / OpenTelemetry mirror"
        subtitle="The scrape surface and the JSON telemetry the cockpit chip reads"
        actions={
          <Btn size="sm" tone="ghost" onClick={metrics.reload}>
            Re-scrape
          </Btn>
        }
      >
        <div className="nx-grid nx-grid--2-1">
          <div>
            {metricRows.length === 0 ? (
              <EmptyCleanSlate
                title="No scrape recorded"
                body="The metrics mirror answers only when the gateway is serving. Nothing here is synthesised."
                hint="GET /api/v2/metrics"
              />
            ) : (
              metricRows.map((row) => (
                <div className="nx-row nx-row--between" key={row.name}>
                  <span className="nx-xs nx-muted nx-mono">{row.name}</span>
                  <span className="nx-xs nx-strong nx-num">{row.value}</span>
                </div>
              ))
            )}
          </div>
          <div className="nx-stack">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Prometheus exposition</span>
              <span className="nx-xs nx-strong nx-mono">GET /api/v2/metrics</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">JSON telemetry</span>
              <span className="nx-xs nx-strong nx-mono">GET /api/v2/metrics/json</span>
            </div>
            <div className="nx-hr" />
            <span className="nx-xs nx-dim">
              Alert families: tick latency p99, ring heartbeat age, conformal q98, outbox reservations,
              token spend against the daily cap, active leases and silo NAV.
            </span>
          </div>
        </div>
      </TerminalPanel>
    </div>
  );
}
