/**
 * Page 13 — Formal proofs, seeded chaos and the Prometheus mirror.
 *
 * Everything on this page is evidence: the exhaustive TLA+ state space, the
 * seeded VOPR fault campaign, the SIGKILL recovery drill and the OS/socket
 * audit. Each action renders its own receipt as a bespoke surface — an operator
 * reads proofs, not payloads.
 */

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  EmptyCleanSlate,
  ErrorBlock,
  Freshness,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  SpecularCard,
  StatusPill,
  TabularValue,
  passTone,
} from "@/design-system/primitives";
import { api } from "@/lib/api";
import { int, latency, num, record, rows, text, titleize, utc } from "@/lib/format";
import { navigate, pagePath, useLocation } from "@/lib/router";
import type { ChaosDrillReceipt, FormalProofState, OsTuneReceipt, VoprCampaignReceipt } from "@/lib/types";
import { useAction, useResource } from "@/lib/use-api";

/* ------------------------------- helpers ------------------------------- */

const SILO_IDS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"] as const;
const RING_IDS = ["ring0", "ring1", "ring2", "ring3"] as const;

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function flagOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function statusTone(status: string, flag: boolean | null): "ok" | "bad" | "neutral" {
  if (flag === true) return "ok";
  if (flag === false) return "bad";
  const upper = status.trim().toUpperCase();
  if (upper === "PASSED" || upper === "PASS" || upper === "TUNED") return "ok";
  if (upper === "FAILED" || upper === "FAIL" || upper === "ERROR") return "bad";
  return "neutral";
}

function flagLabel(flag: boolean | null): string {
  if (flag === true) return "PASS";
  if (flag === false) return "FAIL";
  return "—";
}

function firstOf(source: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = source[key];
    if (value !== null && value !== undefined) return value;
  }
  return null;
}

function firstOfAny(sources: Array<Record<string, unknown>>, keys: string[]): unknown {
  for (const source of sources) {
    const value = firstOf(source, keys);
    if (value !== null && value !== undefined) return value;
  }
  return null;
}

interface ProofRow {
  name: string;
  states: unknown;
  transitions: unknown;
  counterexamples: unknown;
  depth: unknown;
  durationMs: unknown;
  ok: boolean;
}

/* -------------------------------- page -------------------------------- */

export function VerificationPage(): ReactNode {
  const location = useLocation();
  const proofs = useResource<FormalProofState>("formal-proofs", () => api.formalProofs(), 15000);
  const metrics = useResource("metrics", () => api.metrics(), 10000);
  const rerun = useAction<FormalProofState>();

  const state = proofs.data;
  const summary = record(state?.summary);
  const explored = asNumber(summary.states_explored);
  const counterexamples = asNumber(summary.counterexamples);
  const tlaOk = flagOrNull(summary.tla_ok);
  const voprOk = flagOrNull(summary.vopr_ok);
  const tigerbeetleReady = flagOrNull(summary.tigerbeetle_ready);
  const postgresReady = flagOrNull(summary.postgres_ready);
  const tailHedgerReady = flagOrNull(summary.tail_hedger_ready);

  const proofModel = useMemo(() => {
    const tlaRecord = record(state?.tla);
    const specs = rows(tlaRecord.specs);
    const entries = specs.length > 0 ? specs : rows(tlaRecord.entries);
    const list: ProofRow[] = entries.map((row) => {
      const details = record(row.details);
      return {
        name: text(firstOf(row, ["name", "spec", "id"]), "spec"),
        states: firstOf(row, ["states_explored", "states", "total_states"]),
        transitions: firstOf(row, ["transitions", "edges"]),
        counterexamples: firstOf(row, ["counterexamples"]),
        depth: firstOfAny([details, row], ["depth", "max_depth", "terminal_states"]),
        durationMs: firstOf(row, ["duration_ms"]),
        ok: row.ok === true && asNumber(row.counterexamples) === 0,
      };
    });
    return {
      rows: list,
      totalTransitions: firstOf(tlaRecord, ["total_transitions"]),
      totalDurationMs: firstOf(tlaRecord, ["duration_ms"]),
    };
  }, [state]);

  const metricsSnapshot = record(metrics.data);
  const latencyBlock = record(metricsSnapshot.tick_latency);
  const quantiles = record(latencyBlock.quantiles);
  const buckets = rows(latencyBlock.buckets);
  const processBlock = record(metricsSnapshot.process);
  const heartbeatRows = rows(metricsSnapshot.rings);
  const siloRows = rows(metricsSnapshot.silos);
  const hasMetrics =
    buckets.length > 0 ||
    heartbeatRows.length > 0 ||
    siloRows.length > 0 ||
    Object.keys(processBlock).length > 0 ||
    Object.keys(quantiles).length > 0;
  const p50Seconds = asNumber(quantiles.p50);
  const p99Seconds = asNumber(quantiles.p99);
  const p99BudgetOk = flagOrNull(latencyBlock.p99_within_budget);

  const runProofs = (): void => {
    void rerun.run(() => api.formalProofs(true)).then((receipt) => {
      if (receipt) proofs.reload();
    });
  };

  const suiteOk = flagOrNull(state?.ok);

  return (
    <div>
      <SectionHeader
        eyebrow={`ring0 · audit · ${location.workspace} workspace`}
        title="Formal Proofs & Chaos"
        subtitle="Exhaustive TLA+ state-space verification, seeded VOPR fault injection, isolated SIGKILL recovery drills and the low-latency OS/socket audit — each run returns a proof receipt."
        actions={
          <>
            <Freshness generatedAtNs={state?.checked_at_ns} onRefresh={proofs.reload} busy={proofs.loading} />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => navigate(pagePath("control", location.workspace))}
              aria-label="Open the 22-gate go-live certificate in the control room"
            >
              Go-live certificate
            </button>
          </>
        }
      />

      <div className="flex flex-col gap-[14px]">
        <GlassPanel
          title={explored === null ? "TLA+ BFS proof" : `${int(explored)}-state TLA+ BFS proof`}
          eyebrow="sec_010 · exhaustive breadth-first model check"
          actions={
            <button
              type="button"
              className="btn btn-sm"
              onClick={runProofs}
              disabled={rerun.busy}
              aria-label="Re-run the TLA+ breadth-first state-space proof"
            >
              {rerun.busy ? "Exploring state space…" : "Re-run proof BFS"}
            </button>
          }
        >
          {proofs.loading && !state ? <LoadingBlock rows={4} label="Exploring the TLA+ state space" /> : null}
          {proofs.error ? <ErrorBlock message={proofs.error} onRetry={proofs.reload} /> : null}
          {rerun.error ? <ErrorBlock message={rerun.error} onRetry={runProofs} /> : null}

          {state ? (
            <div className="flex flex-col gap-3">
              <div className="grid-auto-180">
                <MetricTile
                  label="States explored"
                  value={<TabularValue value={summary.states_explored} kind="int" />}
                  hint="exhaustive BFS reachability"
                />
                <MetricTile
                  label="Counterexamples"
                  value={<TabularValue value={summary.counterexamples} kind="int" />}
                  tone={counterexamples === null ? "dim" : counterexamples === 0 ? "pos" : "neg"}
                  hint="must be exactly 0"
                />
                <MetricTile
                  label="Transitions"
                  value={<TabularValue value={proofModel.totalTransitions} kind="int" />}
                  hint="state-space edges"
                />
                <MetricTile
                  label="Suite duration"
                  value={<span className="mono tnum">{num(proofModel.totalDurationMs, 2)} ms</span>}
                  hint={`checked ${utc(state.checked_at_ns)} UTC`}
                />
              </div>

              <div className="chip-row">
                <StatusPill
                  value={`TLA+ proof ${flagLabel(tlaOk)}`}
                  tone={passTone(tlaOk)}
                  title={`tla_ok: ${text(summary.tla_ok, "unknown")}`}
                />
                <StatusPill
                  value={`VOPR ${flagLabel(voprOk)}`}
                  tone={passTone(voprOk)}
                  title={`vopr_ok: ${text(summary.vopr_ok, "unknown")}`}
                />
                <StatusPill
                  value={`TigerBeetle ${flagLabel(tigerbeetleReady)}`}
                  tone={passTone(tigerbeetleReady)}
                  title={`tigerbeetle_ready: ${text(summary.tigerbeetle_ready, "unknown")}`}
                />
                <StatusPill
                  value={`PostgreSQL 16 ${flagLabel(postgresReady)}`}
                  tone={passTone(postgresReady)}
                  title={`postgres_ready: ${text(summary.postgres_ready, "unknown")}`}
                />
                <StatusPill
                  value={`Tail hedger ${flagLabel(tailHedgerReady)}`}
                  tone={passTone(tailHedgerReady)}
                  title={`tail_hedger_ready: ${text(summary.tail_hedger_ready, "unknown")}`}
                />
                <StatusPill
                  value={suiteOk === true ? "Proof suite PASS" : suiteOk === false ? "Proof suite FAIL" : "Proof suite unknown"}
                  tone={passTone(suiteOk)}
                  title={`ok: ${text(state.ok, "unknown")}`}
                />
              </div>

              {proofModel.rows.length === 0 ? (
                <EmptyCleanSlate
                  title="No per-spec result rows in the payload"
                  body="The formal verifier returns one row per spec under tla.specs (KillSwitch, OrderLifecycle, Outbox, Reconciliation). An empty array means specs/ has not been harvested on this host yet."
                  action={
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={runProofs}
                      disabled={rerun.busy}
                      aria-label="Re-run the TLA+ state-space proof now"
                    >
                      Re-run proof BFS
                    </button>
                  }
                />
              ) : (
                <div className="scroll-x">
                  <table className="table table-compact">
                    <caption className="sr-only">
                      TLA+ specification results: states explored, transitions, counterexamples, depth and duration
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Specification</th>
                        <th scope="col" className="num">
                          States
                        </th>
                        <th scope="col" className="num">
                          Transitions
                        </th>
                        <th scope="col" className="num">
                          Counterexamples
                        </th>
                        <th scope="col" className="num">
                          Depth
                        </th>
                        <th scope="col" className="num">
                          Duration
                        </th>
                        <th scope="col">Verdict</th>
                      </tr>
                    </thead>
                    <tbody>
                      {proofModel.rows.map((row) => (
                        <tr key={row.name}>
                          <td className="mono text-[11.5px]">{row.name}</td>
                          <td className="num">
                            <TabularValue value={row.states} kind="int" />
                          </td>
                          <td className="num">
                            <TabularValue value={row.transitions} kind="int" />
                          </td>
                          <td className="num">
                            <TabularValue value={row.counterexamples} kind="int" />
                          </td>
                          <td className="num">
                            <TabularValue value={row.depth} kind="int" />
                          </td>
                          <td className="num">
                            <span className="mono tnum">{num(row.durationMs, 2)} ms</span>
                          </td>
                          <td>
                            <StatusPill value={row.ok ? "PASS" : "FAIL"} tone={passTone(row.ok)} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ) : null}

          {!state && !proofs.loading && !proofs.error ? (
            <EmptyCleanSlate
              title="No formal proof state returned"
              body="The gateway did not return tla, vopr or backend readiness. Re-run the BFS once the Ring 0 formal verifier is attached."
            />
          ) : null}
        </GlassPanel>

        <div className="grid-auto-300">
          <VoprPanel />
          <ChaosPanel />
          <OsTunePanel />
        </div>

        <GlassPanel
          title="Prometheus / OpenTelemetry mirror"
          eyebrow="sec_010 · telemetry scrape"
          actions={
            <Freshness
              generatedAtNs={metricsSnapshot.generated_at_ns}
              onRefresh={metrics.reload}
              busy={metrics.loading}
            />
          }
        >
          {metrics.loading && !metrics.data ? <LoadingBlock rows={3} label="Scraping the metrics exporter" /> : null}
          {metrics.error ? <ErrorBlock message={metrics.error} onRetry={metrics.reload} /> : null}

          {metrics.data && hasMetrics ? (
            <div className="flex flex-col gap-3">
              <div className="chip-row">
                <span className="pill pill-info mono">/api/v2/metrics</span>
                <span className="pill mono">/metrics</span>
                <span className="pill mono" title="exporter node">
                  {text(metricsSnapshot.node, "nios-core")}
                </span>
                <span className="pill mono" title="exporter version">
                  v{text(metricsSnapshot.version, "—")}
                </span>
              </div>

              <div className="grid-auto-240">
                <SpecularCard
                  title="Ring 1 tick latency"
                  subtitle="tick-to-intent · sec_010 budget 0.14 ms p99"
                >
                  <div className="mt-2 flex flex-col gap-1.5">
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">p50</span>
                      <span className="mono tnum text-[12px]">
                        {latency(p50Seconds === null ? null : p50Seconds * 1000)}
                      </span>
                    </div>
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">p99</span>
                      <span className="row gap-2">
                        <span className="mono tnum text-[12px]">
                          {latency(p99Seconds === null ? null : p99Seconds * 1000)}
                        </span>
                        <StatusPill
                          value={p99BudgetOk === null ? "N/A" : p99BudgetOk ? "in budget" : "over budget"}
                          tone={passTone(p99BudgetOk)}
                        />
                      </span>
                    </div>
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">Samples</span>
                      <TabularValue value={latencyBlock.count} kind="int" className="mono text-[11px]" />
                    </div>
                  </div>
                  <div className="mt-3 border-t border-[var(--line)] pt-2">
                    <p className="eyebrow mb-1.5">Latency histogram</p>
                    <BucketBars className="mt-1" buckets={buckets} />
                  </div>
                </SpecularCard>

                <SpecularCard title="Process memory" subtitle="cockpit gateway footprint">
                  <div className="mt-2 flex flex-col gap-1.5">
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">RSS</span>
                      <span className="mono tnum text-[12px]">{num(processBlock.rss_mb, 1)} MB</span>
                    </div>
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">Python heap</span>
                      <span className="mono tnum text-[12px]">{num(processBlock.python_heap_mb, 1)} MB</span>
                    </div>
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">Threads</span>
                      <TabularValue value={processBlock.threads} kind="int" className="mono text-[11px]" />
                    </div>
                    <div className="row-between">
                      <span className="mono text-[10px] tracking-[0.1em] text-[var(--dim)] uppercase">Uptime</span>
                      <span className="mono tnum text-[11px]">{num(metricsSnapshot.uptime_seconds, 0)} s</span>
                    </div>
                  </div>
                </SpecularCard>

                <SpecularCard title="Ring heartbeats" subtitle="shm://nios-ring-heartbeats">
                  <div className="mt-2 flex flex-col gap-1.5">
                    {heartbeatRows.length === 0 ? (
                      <p className="mono text-[10.5px] text-[var(--dim)]">No heartbeat rows in this snapshot.</p>
                    ) : (
                      heartbeatRows.map((row) => {
                        const alive = row.alive === true;
                        const age = asNumber(row.heartbeat_age_ms);
                        return (
                          <div key={`${text(row.ring)}-${text(row.worker)}`} className="row-between">
                            <span className="row min-w-0 gap-2">
                              <span className={`pill-dot ${alive ? "pos" : "neg"}`} aria-hidden="true" />
                              <span className="mono truncate text-[10.5px]">
                                {text(row.worker, `ring ${text(row.ring, "?")}`)}
                              </span>
                            </span>
                            <span className="mono tnum flex-none text-[11px]">
                              {alive && age !== null && age >= 0 ? `${num(age, 1)} ms` : alive ? "alive" : "down"}
                            </span>
                          </div>
                        );
                      })
                    )}
                  </div>
                </SpecularCard>

                <SpecularCard title="Silo NAV" subtitle="four silos · 128-bit micro-units">
                  <div className="mt-2 flex flex-col gap-1.5">
                    {siloRows.length === 0 ? (
                      <p className="mono text-[10.5px] text-[var(--dim)]">No silo rows in this snapshot.</p>
                    ) : (
                      siloRows.map((row) => (
                        <div key={text(row.silo, "silo")} className="row-between">
                          <span className="mono truncate text-[10.5px]">{text(row.silo, "silo")}</span>
                          <TabularValue
                            value={row.nav_micros}
                            kind="money"
                            className="mono flex-none text-[11px]"
                          />
                        </div>
                      ))
                    )}
                  </div>
                </SpecularCard>
              </div>
            </div>
          ) : null}

          {metrics.data && !hasMetrics ? (
            <EmptyCleanSlate
              title="Metrics exporter returned an empty snapshot"
              body="The gateway exposes Prometheus text at /api/v2/metrics (alias /metrics) and JSON at /api/v2/metrics/json. An empty family set means no Ring 1 ticks, no heartbeat table and no ledger balances have been observed since boot."
            />
          ) : null}
        </GlassPanel>
      </div>
    </div>
  );
}

/* ------------------------------ VOPR panel ------------------------------ */

function VoprPanel(): ReactNode {
  const [seed, setSeed] = useState("2026");
  const [steps, setSteps] = useState("1000");
  const action = useAction<VoprCampaignReceipt>();

  const seedValue = Number(seed);
  const stepsValue = Number(steps);
  const valid = Number.isFinite(seedValue) && Number.isFinite(stepsValue) && stepsValue >= 10;

  const receipt = record(action.receipt);
  const faultCounts = record(firstOf(receipt, ["fault_counts", "faults"]));
  const faultEntries = Object.entries(faultCounts)
    .map(([key, value]) => ({ key, count: asNumber(value) }))
    .filter((entry): entry is { key: string; count: number } => entry.count !== null);
  const maxFault = Math.max(1, ...faultEntries.map((entry) => entry.count));
  const ok = flagOrNull(firstOf(receipt, ["ok", "all_invariants_hold"]));
  const status = text(receipt.status, action.receipt ? "UNKNOWN" : "");

  const run = (): void => {
    if (!valid) return;
    void action.run(() => api.vopr({ seed: seedValue, steps: stepsValue }));
  };

  return (
    <GlassPanel
      title="Seeded VOPR fault campaign"
      eyebrow="sec_009 · deterministic power-cycle simulation"
      actions={
        <button
          type="button"
          className="btn btn-sm btn-accent"
          onClick={run}
          disabled={action.busy || !valid}
          aria-label="Run the seeded VOPR fault injection campaign"
        >
          {action.busy ? "Injecting…" : "Run campaign"}
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2">
          <label className="field">
            <span className="field-label">Seed</span>
            <input
              className="input tnum"
              type="number"
              inputMode="numeric"
              value={seed}
              onChange={(event) => setSeed(event.target.value)}
              aria-label="VOPR deterministic seed"
            />
          </label>
          <label className="field">
            <span className="field-label">Steps</span>
            <input
              className="input tnum"
              type="number"
              inputMode="numeric"
              min={10}
              value={steps}
              onChange={(event) => setSteps(event.target.value)}
              aria-label="VOPR simulation steps"
            />
          </label>
        </div>

        {action.error ? <ErrorBlock message={action.error} onRetry={run} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2">
            <div className="chip-row">
              <StatusPill value={status} tone={statusTone(status, ok)} />
              <span className="pill mono">
                seed <TabularValue value={receipt.seed} kind="int" />
              </span>
              <span className="pill mono">
                steps <TabularValue value={receipt.steps} kind="int" />
              </span>
              <span className="pill mono">
                faults <TabularValue value={receipt.injected_faults} kind="int" />
              </span>
              <span className="pill mono">
                <TabularValue value={receipt.duration_ms} digits={1} /> ms
              </span>
            </div>

            <div className="row-between text-[11px]">
              <span className="text-[var(--muted)]">Fault histogram</span>
              <span className="mono text-[10px] text-[var(--dim)]">
                <TabularValue value={receipt.power_cycles} kind="int" /> power cycles ·{" "}
                <TabularValue value={receipt.commits} kind="int" /> commits
              </span>
            </div>

            {faultEntries.length === 0 ? (
              <p className="mono text-[10.5px] text-[var(--dim)]">
                No fault families were injected in this campaign.
              </p>
            ) : (
              <div className="flex flex-col gap-1.5">
                {faultEntries.map((entry) => (
                  <div key={entry.key} className="flex flex-col gap-1">
                    <div className="row-between">
                      <span className="mono text-[10.5px] tracking-[0.04em] text-[var(--muted)]">
                        {titleize(entry.key)}
                      </span>
                      <TabularValue value={entry.count} kind="int" className="mono text-[10.5px]" />
                    </div>
                    <div
                      className="h-1.5 w-full overflow-hidden rounded-full bg-[rgba(148,163,184,0.14)]"
                      role="img"
                      aria-label={`${titleize(entry.key)}: ${int(entry.count)} injected faults`}
                    >
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${Math.max(3, Math.round((entry.count / maxFault) * 100))}%`,
                          background: "var(--accent-2)",
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="row-between border-t border-[var(--line)] pt-1.5">
              <span className="mono text-[10px] text-[var(--dim)]">overdrafts</span>
              <TabularValue value={receipt.overdrafts} kind="int" className="mono text-[10.5px]" />
            </div>
            <div className="row-between">
              <span className="mono text-[10px] text-[var(--dim)]">duplicate executions</span>
              <TabularValue value={receipt.duplicate_executions} kind="int" className="mono text-[10.5px]" />
            </div>
          </div>
        ) : (
          <p className="text-[11.5px] text-[var(--dim)]">
            Runs the deterministic TigerBeetle-style VOPR campaign: seeded power cycles, reservation commits and voids
            with zero overdrafts and zero duplicate transfers.
          </p>
        )}
      </div>
    </GlassPanel>
  );
}

/* ------------------------------ chaos panel ------------------------------ */

interface InvariantRow {
  key: string;
  label: string;
  detail: string;
  pass: boolean | null;
}

function ChaosPanel(): ReactNode {
  const [iterations, setIterations] = useState("12");
  const [holdMs, setHoldMs] = useState("25");
  const [seed, setSeed] = useState("7");
  const [silo, setSilo] = useState<string>(SILO_IDS[0]);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const action = useAction<ChaosDrillReceipt>();

  const iterationsValue = Number(iterations);
  const holdValue = Number(holdMs);
  const seedValue = Number(seed);
  const valid =
    Number.isFinite(iterationsValue) &&
    iterationsValue >= 8 &&
    Number.isFinite(holdValue) &&
    holdValue >= 10 &&
    Number.isFinite(seedValue) &&
    symbol.trim() !== "";

  const receipt = record(action.receipt);
  const proof = record(firstOf(receipt, ["proof"]));
  const invariants = record(firstOf(receipt, ["invariants"]));
  const pending = asNumber(firstOf(proof, ["pending_after"]));
  const duplicates = asNumber(firstOf(proof, ["duplicate_transfer_ids"]));
  const debits = asNumber(firstOf(proof, ["sum_debits_micros"]));
  const credits = asNumber(firstOf(proof, ["sum_credits_micros"]));
  const leaseMode = text(firstOf(proof, ["lease_mode_after_reboot"]), "—");

  const rows_: InvariantRow[] = [
    {
      key: "pending_after",
      label: "Reservations pending after reboot",
      detail: `pending_after = ${pending === null ? "—" : int(pending)}`,
      pass: pending === null ? null : pending === 0,
    },
    {
      key: "duplicate_transfer_ids",
      label: "Duplicate transfer ids",
      detail: `duplicate_transfer_ids = ${duplicates === null ? "—" : int(duplicates)}`,
      pass: duplicates === null ? null : duplicates === 0,
    },
    {
      key: "stale_lease",
      label: "Stale lease swept, never inherited",
      detail: `lease_mode_after_reboot = ${leaseMode}`,
      pass:
        flagOrNull(invariants.stale_lease_swept_to_disarmed) ??
        (leaseMode === "—" ? null : leaseMode.toUpperCase() === "DISARMED"),
    },
    {
      key: "double_entry",
      label: "Debits equal credits",
      detail: `debits ${debits === null ? "—" : int(debits)} · credits ${credits === null ? "—" : int(credits)}`,
      pass:
        flagOrNull(invariants.double_entry_conservation) ??
        (debits === null || credits === null ? null : debits === credits),
    },
  ];

  const ok = flagOrNull(firstOf(receipt, ["all_invariants_hold"]));
  const status = text(receipt.status, "");

  const run = (): void => {
    if (!valid) return;
    void action.run(() =>
      api.chaosDrill({
        iterations: iterationsValue,
        hold_ms: holdValue,
        seed: seedValue,
        silo,
        symbol: symbol.trim().toUpperCase(),
      }),
    );
  };

  return (
    <GlassPanel
      title="Isolated SIGKILL recovery drill"
      eyebrow="sec_009 · crash-atomic outbox"
      actions={
        <button
          type="button"
          className="btn btn-sm btn-accent"
          onClick={run}
          disabled={action.busy || !valid}
          aria-label="Run the isolated SIGKILL recovery drill"
        >
          {action.busy ? "Killing worker…" : "Run drill"}
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2">
          <label className="field">
            <span className="field-label">Iterations</span>
            <input
              className="input tnum"
              type="number"
              inputMode="numeric"
              min={8}
              value={iterations}
              onChange={(event) => setIterations(event.target.value)}
              aria-label="Chaos drill iteration count"
            />
          </label>
          <label className="field">
            <span className="field-label">Hold ms</span>
            <input
              className="input tnum"
              type="number"
              inputMode="numeric"
              min={10}
              value={holdMs}
              onChange={(event) => setHoldMs(event.target.value)}
              aria-label="Chaos drill in-flight hold milliseconds"
            />
          </label>
          <label className="field">
            <span className="field-label">Seed</span>
            <input
              className="input tnum"
              type="number"
              inputMode="numeric"
              value={seed}
              onChange={(event) => setSeed(event.target.value)}
              aria-label="Chaos drill deterministic seed"
            />
          </label>
          <label className="field">
            <span className="field-label">Silo</span>
            <select
              className="select"
              value={silo}
              onChange={(event) => setSilo(event.target.value)}
              aria-label="Chaos drill target silo"
            >
              {SILO_IDS.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="field">
          <span className="field-label">Symbol</span>
          <input
            className="input mono"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            aria-label="Chaos drill target symbol"
          />
        </label>

        {action.error ? <ErrorBlock message={action.error} onRetry={run} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2">
            <div className="chip-row">
              <StatusPill value={status} tone={statusTone(status, ok)} />
              <span className="pill mono">
                <TabularValue value={receipt.elapsed_ms} digits={1} /> ms
              </span>
              <span className="pill mono">
                <TabularValue value={receipt.iterations} kind="int" /> iterations
              </span>
            </div>
            <ul className="flex flex-col">
              {rows_.map((row) => (
                <li
                  key={row.key}
                  className="flex items-start justify-between gap-3 border-b border-[var(--line)] py-1.5 last:border-0"
                >
                  <div className="min-w-0">
                    <p className="text-[11.5px] text-[var(--ink-soft)]">{row.label}</p>
                    <p className="mono tnum text-[10px] break-words text-[var(--dim)]">{row.detail}</p>
                  </div>
                  <StatusPill
                    value={row.pass === null ? "N/A" : row.pass ? "PASS" : "FAIL"}
                    tone={passTone(row.pass)}
                  />
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="text-[11.5px] text-[var(--dim)]">
            SIGKILLs a worker mid-outbox inside a throwaway workspace, reboots it and proves no reservation is
            orphaned, no transfer id is duplicated and an expired lease is swept to DISARMED.
          </p>
        )}
      </div>
    </GlassPanel>
  );
}

/* ------------------------------ OS tune panel ------------------------------ */

function OsTunePanel(): ReactNode {
  const [ring, setRing] = useState<string>(RING_IDS[1]);
  const [apply, setApply] = useState(false);
  const action = useAction<OsTuneReceipt>();

  const receipt = record(action.receipt);
  const checks = rows(firstOf(receipt, ["checks"]));
  const status = text(receipt.status, "");
  const tuned = flagOrNull(firstOf(receipt, ["tuned"]));

  const run = (): void => {
    void action.run(() => api.osTune({ apply, ring }));
  };

  return (
    <GlassPanel
      title="OS low-latency tuning & socket audit"
      eyebrow="sec_010 · kernel + socket budget"
      actions={
        <button
          type="button"
          className="btn btn-sm"
          onClick={run}
          disabled={action.busy}
          aria-label="Audit OS low-latency tuning and sockets"
        >
          {action.busy ? "Auditing…" : apply ? "Apply & audit" : "Audit"}
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-2 items-end gap-2">
          <label className="field">
            <span className="field-label">Ring</span>
            <select
              className="select"
              value={ring}
              onChange={(event) => setRing(event.target.value)}
              aria-label="OS tuning target ring"
            >
              {RING_IDS.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label className="check pb-2">
            <input
              type="checkbox"
              checked={apply}
              onChange={(event) => setApply(event.target.checked)}
              aria-label="Apply the recommended OS tuning instead of only auditing"
            />
            <span>Apply recommendations</span>
          </label>
        </div>

        {action.error ? <ErrorBlock message={action.error} onRetry={run} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2">
            <div className="chip-row">
              <StatusPill value={status} tone={statusTone(status, null)} />
              <StatusPill
                value={tuned === true ? "TUNED" : "AUDIT ONLY"}
                tone={tuned === true ? "info" : "neutral"}
                title={`tuned: ${text(receipt.tuned, "unknown")}`}
              />
              <span className="pill mono">
                applied <TabularValue value={receipt.applied_count} kind="int" />
              </span>
              <span className="pill mono">
                recommended <TabularValue value={receipt.recommended_count} kind="int" />
              </span>
              <span className="pill mono">{text(receipt.profile, "—")}</span>
            </div>

            {checks.length === 0 ? (
              <EmptyCleanSlate
                title="No OS tuning checks were returned"
                body="The audit returns one row per socket, timer, affinity and sysctl probe. An empty list means the probe could not run on this host."
              />
            ) : (
              <div className="scroll-x">
                <table className="table table-compact">
                  <caption className="sr-only">OS and socket tuning checks with status, detail and command</caption>
                  <thead>
                    <tr>
                      <th scope="col">Check</th>
                      <th scope="col">Status</th>
                      <th scope="col">Detail</th>
                      <th scope="col">Command</th>
                    </tr>
                  </thead>
                  <tbody>
                    {checks.map((row, index) => {
                      const checkStatus = text(row.status, "UNKNOWN");
                      const checkUpper = checkStatus.toUpperCase();
                      return (
                        <tr key={`${text(row.id, text(row.check, "check"))}-${index}`}>
                          <td className="text-[11.5px]">{text(row.label, text(row.check, text(row.id, "—")))}</td>
                          <td>
                            <StatusPill
                              value={checkStatus}
                              tone={checkUpper === "APPLIED" ? "ok" : checkUpper === "FAILED" || checkUpper === "ERROR" ? "bad" : "warn"}
                            />
                          </td>
                          <td className="text-[10.5px] text-[var(--dim)]">{text(row.detail)}</td>
                          <td className="mono text-[10px] break-all">{text(row.command, "—")}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <p className="text-[11.5px] text-[var(--dim)]">
            Audits CPU affinity, SCHED_FIFO, huge pages, SHM alignment and TCP_NODELAY/QUICKACK per ring. Applying writes
            the recommended profile; auditing never mutates the host.
          </p>
        )}
      </div>
    </GlassPanel>
  );
}

/* ------------------------------ bucket bars ------------------------------ */

function BucketBars({
  buckets,
  className = "",
}: {
  buckets: Array<Record<string, unknown>>;
  className?: string;
}): ReactNode {
  const points = buckets
    .map((bucket) => ({ le: asNumber(bucket.le), count: asNumber(bucket.count) }))
    .filter((point): point is { le: number; count: number } => point.le !== null && point.count !== null);

  if (points.length === 0) {
    return <p className="mono text-[10.5px] text-[var(--dim)]">No histogram samples in this snapshot.</p>;
  }

  const max = Math.max(1, ...points.map((point) => point.count));

  return (
    <div className={`flex flex-col gap-1 ${className}`.trim()}>
      {points.map((point) => (
        <div key={point.le} className="flex items-center gap-2">
          <span className="mono tnum w-[62px] flex-none text-right text-[9.5px] text-[var(--dim)]">
            {num(point.le * 1000, 3)}ms
          </span>
          <div
            className="h-1.5 flex-1 overflow-hidden rounded-full bg-[rgba(148,163,184,0.14)]"
            role="img"
            aria-label={`${int(point.count)} samples at or below ${num(point.le * 1000, 3)} milliseconds`}
          >
            <div
              className="h-full rounded-full"
              style={{
                width: `${Math.max(2, Math.round((point.count / max) * 100))}%`,
                background: "var(--accent)",
              }}
            />
          </div>
          <TabularValue
            value={point.count}
            kind="int"
            className="w-[54px] flex-none text-right text-[9.5px] text-[var(--dim)]"
          />
        </div>
      ))}
    </div>
  );
}
