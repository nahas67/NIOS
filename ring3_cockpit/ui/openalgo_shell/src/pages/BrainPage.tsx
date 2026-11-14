/**
 * Brain & contagion X-ray (`/brain`, alias `/xray`).
 *
 * One observed process matrix and two on-demand cognition receipts. Nothing on
 * this page is reconstructed locally: the process table is read from the
 * supervisor heartbeat, and every cascade or committee turn renders only the
 * hashed fields its receipt returned.
 */

import { useState, type ReactNode } from "react";
import { Play, RotateCcw, Square, Zap } from "lucide-react";
import { ContagionFlowGraph } from "@/charts/ContagionFlowGraph";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabularValue,
  passTone,
} from "@/design-system/primitives";
import { api } from "@/lib/api";
import { bool, int, latency, num, price, record, text, titleize } from "@/lib/format";
import { useLocation } from "@/lib/router";
import { useAction, useResource } from "@/lib/use-api";
import type { CommitteeDebateReceipt, JsonRecord, MiroFishShockReceipt } from "@/lib/types";

const SILOS = ["INDIA_ZERODHA_DHAN", "CRYPTO_BINANCE", "US_IBKR", "FX_MT5"] as const;

const HEARTBEAT_TONE: Record<"pos" | "warn" | "neg" | "dim", string> = {
  pos: "text-[var(--pos)]",
  warn: "text-[var(--warn)]",
  neg: "text-[var(--neg)]",
  dim: "text-[var(--dim)]",
};

function heartbeatTone(age: number | null | undefined): "pos" | "warn" | "neg" | "dim" {
  if (typeof age !== "number" || !Number.isFinite(age)) return "dim";
  if (age < 1_000) return "pos";
  if (age < 3_000) return "warn";
  return "neg";
}

function numberField(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function workspaceLabel(workspace: "base" | "paper" | "live"): string {
  if (workspace === "live") return "LIVE VAULT";
  if (workspace === "paper") return "PAPER SANDBOX";
  return "GATEWAY";
}

export function BrainPage(): ReactNode {
  const { workspace } = useLocation();
  const state = useResource("cockpit-state", () => api.state(), 6_000);
  const brain = useResource("brain-envelope", () => api.brain());

  const [shockSilo, setShockSilo] = useState<string>(SILOS[0]);
  const [shockSymbol, setShockSymbol] = useState("NIFTY");
  const [shockBpsValue, setShockBpsValue] = useState("85");
  const [sentiment, setSentiment] = useState("risk_off");
  const [volatilityRatio, setVolatilityRatio] = useState("1.35");
  const [shockSpread, setShockSpread] = useState("4.2");
  const [gammaProximity, setGammaProximity] = useState("0.25");
  const [shockFormError, setShockFormError] = useState<string | null>(null);

  const [committeeSilo, setCommitteeSilo] = useState<string>(SILOS[0]);
  const [committeeSymbol, setCommitteeSymbol] = useState("AUTO");
  const [committeeSpread, setCommitteeSpread] = useState("4.0");
  const [committeeFormError, setCommitteeFormError] = useState<string | null>(null);

  const [restartRing, setRestartRing] = useState("0");
  const [lastSupervisorAction, setLastSupervisorAction] = useState<{
    action: "start" | "stop" | "restart_ring";
    ring?: number;
  }>({ action: "start" });

  const supervisor = useAction<JsonRecord>();
  const shock = useAction<MiroFishShockReceipt>();
  const committee = useAction<CommitteeDebateReceipt>();

  const processTable = state.data?.ring_processes ?? null;
  const supervisorReceipt = supervisor.receipt;
  const cascade = shock.receipt;
  const debate = committee.receipt;
  const brainEnvelope = record(brain.data);

  const runShock = async () => {
    const shockBps = numberField(shockBpsValue);
    const ratio = numberField(volatilityRatio);
    const spread = numberField(shockSpread);
    const gamma = numberField(gammaProximity);
    if (shockSymbol.trim() === "") {
      setShockFormError("Enter the symbol the shock is applied to.");
      return;
    }
    if (shockBps === null || ratio === null || spread === null || gamma === null) {
      setShockFormError("Shock bps, volatility ratio, spread bps and gamma proximity must all be numbers.");
      return;
    }
    setShockFormError(null);
    await shock.run(() =>
      api.mirofishShock({
        symbol: shockSymbol.trim().toUpperCase(),
        silo: shockSilo,
        shock_bps: shockBps,
        sentiment_tag: sentiment.trim(),
        volatility_ratio: ratio,
        spread_bps: spread,
        gamma_flip_proximity: gamma,
      }),
    );
  };

  const runCommittee = async () => {
    const spread = numberField(committeeSpread);
    if (committeeSymbol.trim() === "") {
      setCommitteeFormError("Enter a symbol, or AUTO to take the top-ranked candidate.");
      return;
    }
    if (spread === null) {
      setCommitteeFormError("Spread must be a number in basis points.");
      return;
    }
    setCommitteeFormError(null);
    await committee.run(() =>
      api.committeeDebate({
        silo: committeeSilo,
        symbol: committeeSymbol.trim().toUpperCase(),
        spread_bps: spread,
      }),
    );
  };

  const runSupervisor = async (action: "start" | "stop" | "restart_ring", ringIndex?: number) => {
    setLastSupervisorAction({ action, ring: ringIndex });
    const receipt = await supervisor.run(() => api.supervisor(action, ringIndex));
    if (receipt !== null) state.reload();
  };

  const retrySupervisor = () => {
    void runSupervisor(lastSupervisorAction.action, lastSupervisorAction.ring);
  };

  const rings = processTable?.rings ?? [];
  const aliveRings = rings.filter((entry) => entry.alive).length;

  return (
    <>
      <SectionHeader
        title="Brain & Contagion X-Ray"
        eyebrow="ring2_brain · bicameral cognition"
        subtitle="Ring OS process isolation, the five-archetype MiroFish cascade and the grounded System 2 committee transcript. Ring 2 proposes; it never holds a broker key."
        actions={
          <div className="row gap-1.5">
            <StatusPill value={workspaceLabel(workspace)} tone={workspace === "live" ? "bad" : "accent"} />
            {processTable ? (
              <StatusPill
                value={`${int(aliveRings)}/${int(rings.length)} rings alive`}
                tone={rings.length > 0 && aliveRings === rings.length ? "ok" : "warn"}
                dot={false}
              />
            ) : null}
          </div>
        }
      />

      <div className="flex flex-col gap-[14px]">
        <GlassPanel
          title="Ring OS process isolation matrix"
          eyebrow="four physically separate OS processes · SHM heartbeats"
          actions={
            <div className="row gap-1.5">
              <StatusPill
                value={
                  processTable
                    ? processTable.running
                      ? "SUPERVISOR RUNNING"
                      : "SUPERVISOR IDLE"
                    : "SUPERVISOR UNKNOWN"
                }
                tone={processTable ? (processTable.running ? "ok" : "neutral") : "neutral"}
              />
              <StatusPill
                value={`${int(processTable?.distinct_os_processes)} distinct pids`}
                tone={processTable?.distinct_os_processes === 4 ? "ok" : "neutral"}
                dot={false}
              />
            </div>
          }
          footer={
            <div className="row-between flex-wrap gap-2">
              <div className="row gap-2">
                <span className="eyebrow">bicameral envelope</span>
                {brain.loading && !brain.data ? (
                  <span className="mono text-[10px] text-[var(--dim)]">reading…</span>
                ) : brain.error ? (
                  <StatusPill value="summary unavailable" tone="neutral" dot={false} title={brain.error} />
                ) : (
                  <StatusPill
                    value={`${int(Object.keys(brainEnvelope).length)} sections`}
                    tone="info"
                    dot={false}
                  />
                )}
              </div>
              {brain.error ? (
                <span className="mono text-[9.5px] text-[var(--faint)]">
                  shock and committee call their control endpoints directly
                </span>
              ) : (
                <CopyHash value={brainEnvelope.sha256_digest} label="copy brain envelope digest" />
              )}
            </div>
          }
        >
          {state.error && processTable ? (
            <div className="mb-2">
              <ErrorBlock message={state.error} onRetry={state.reload} />
            </div>
          ) : null}

          {!processTable ? (
            state.error ? (
              <ErrorBlock message={state.error} onRetry={state.reload} />
            ) : state.loading ? (
              <LoadingBlock rows={5} label="Reading the ring process table" />
            ) : (
              <EmptyCleanSlate
                title="No process table published"
                body="The cockpit state did not carry ring_processes. The shock simulator and committee below still call their control endpoints directly."
                action={
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={state.reload}
                    aria-label="Refresh the ring process table"
                  >
                    Refresh process table
                  </button>
                }
              />
            )
          ) : processTable.available === false ? (
            <EmptyCleanSlate
              title="Four-ring process table unavailable"
              body={text(
                processTable.reason,
                "This gateway build runs the rings in one process, so no per-ring OS table exists.",
              )}
            />
          ) : (
            <>
              <div className="scroll-x">
                <table className="table table-compact">
                  <caption className="sr-only">
                    Ring OS process isolation matrix: worker, PID, liveness, heartbeat age, restarts, CPU core
                    and last exit code for each of the four rings.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Ring</th>
                      <th scope="col">Worker</th>
                      <th scope="col">PID</th>
                      <th scope="col">Alive</th>
                      <th scope="col">Heartbeat</th>
                      <th scope="col">Restarts</th>
                      <th scope="col">CPU core</th>
                      <th scope="col">Exit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rings.map((process) => {
                      const alive = process.alive ? "ALIVE" : process.spawned ? "DEAD" : "NOT SPAWNED";
                      const aliveTone = process.alive ? "ok" : process.spawned ? "bad" : "neutral";
                      const beatTone = heartbeatTone(process.heartbeat_age_ms);
                      const exitTone =
                        process.exit_code === null
                          ? "text-[var(--dim)]"
                          : process.exit_code === 0
                            ? "text-[var(--pos)]"
                            : "text-[var(--neg)]";
                      return (
                        <tr key={process.index}>
                          <td>
                            <span className="mono tnum text-[10.5px] text-[var(--accent)]">
                              RING {int(process.index)}
                            </span>
                          </td>
                          <td>
                            <span className="mono text-[10.5px] text-[var(--ink-soft)]">{process.worker}</span>
                          </td>
                          <td className="num">
                            <TabularValue value={process.pid} kind="int" />
                          </td>
                          <td>
                            <StatusPill value={alive} tone={aliveTone} />
                          </td>
                          <td className="num">
                            <span className={`mono tnum ${HEARTBEAT_TONE[beatTone]}`}>
                              {process.heartbeat_age_ms === null
                                ? "—"
                                : `${num(process.heartbeat_age_ms, 0)} ms`}
                            </span>
                          </td>
                          <td className="num">
                            <TabularValue value={process.restarts} kind="int" />
                          </td>
                          <td className="num">
                            <TabularValue value={process.cpu_core} kind="int" />
                          </td>
                          <td className="num">
                            <span className={`mono tnum ${exitTone}`}>
                              <TabularValue value={process.exit_code} kind="int" />
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="mt-3 flex flex-wrap items-end gap-2 rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--panel-sunken)] p-3">
                <div className="min-w-[180px]">
                  <p className="eyebrow">supervisor control</p>
                  <p className="mt-0.5 text-[11px] text-[var(--dim)]">
                    Ring 3 supervises processes; it never signs an order or reads a broker key.
                  </p>
                </div>
                <div className="ml-auto flex flex-wrap items-end gap-2">
                  <label className="field">
                    <span className="field-label">Ring to restart</span>
                    <select
                      className="select"
                      value={restartRing}
                      onChange={(event) => setRestartRing(event.target.value)}
                      aria-label="Ring index to restart"
                    >
                      {[0, 1, 2, 3].map((index) => (
                        <option key={index} value={String(index)}>
                          Ring {index}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void runSupervisor("start")}
                    disabled={supervisor.busy}
                    aria-label="Start all four ring workers"
                  >
                    <Play size={12} aria-hidden="true" />
                    Start all
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void runSupervisor("restart_ring", Number(restartRing))}
                    disabled={supervisor.busy || processTable.running !== true}
                    title={
                      processTable.running === true
                        ? undefined
                        : "Start the supervisor before restarting a ring"
                    }
                    aria-label={`Crash-restart ring ${restartRing}`}
                  >
                    <RotateCcw size={12} aria-hidden="true" />
                    Restart ring
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => void runSupervisor("stop")}
                    disabled={supervisor.busy || processTable.running !== true}
                    title={
                      processTable.running === true ? undefined : "The supervisor is not running"
                    }
                    aria-label="Stop all four ring workers"
                  >
                    <Square size={12} aria-hidden="true" />
                    Stop all
                  </button>
                </div>
              </div>

              {supervisor.error ? (
                <div className="mt-2">
                  <ErrorBlock message={supervisor.error} onRetry={retrySupervisor} />
                </div>
              ) : null}

              {supervisorReceipt ? (
                <div className="mt-2 grid grid-cols-2 gap-2 rounded-[var(--r-sm)] border border-[var(--line)] bg-[rgba(4,7,13,0.6)] px-3 py-2 md:grid-cols-4">
                  <div>
                    <p className="eyebrow">action</p>
                    <p className="mono text-[11px] text-[var(--ink-soft)]">
                      {titleize(text(supervisorReceipt.action, "—"))}
                    </p>
                  </div>
                  <div>
                    <p className="eyebrow">scope</p>
                    <p className="mono tnum text-[11px] text-[var(--ink-soft)]">
                      {supervisorReceipt.ring_index === undefined || supervisorReceipt.ring_index === null
                        ? "all rings"
                        : `ring ${int(supervisorReceipt.ring_index)}`}
                    </p>
                  </div>
                  <div>
                    <p className="eyebrow">pid transition</p>
                    <p className="mono tnum text-[11px] text-[var(--ink-soft)]">
                      <TabularValue value={supervisorReceipt.previous_pid} kind="int" />
                      <span className="px-1 text-[var(--dim)]" aria-hidden="true">
                        →
                      </span>
                      <TabularValue value={supervisorReceipt.new_pid} kind="int" />
                    </p>
                  </div>
                  <div>
                    <p className="eyebrow">applied</p>
                    <div className="mt-0.5 row gap-1.5">
                      <StatusPill
                        value={bool(supervisorReceipt.changed) ? "CHANGED" : "NO CHANGE"}
                        tone={bool(supervisorReceipt.changed) ? "ok" : "neutral"}
                      />
                      <StatusPill
                        value={bool(supervisorReceipt.pid_changed) ? "NEW PID" : "PID STABLE"}
                        tone={bool(supervisorReceipt.pid_changed) ? "info" : "neutral"}
                      />
                    </div>
                  </div>
                </div>
              ) : null}
            </>
          )}
        </GlassPanel>

        <div className="split-2">
          <GlassPanel
            title="MiroFish shock simulator"
            eyebrow="five archetypes · three ticks · hashed cascade"
            actions={
              cascade ? (
                <StatusPill
                  value={`${int(cascade.archetype_count)} archetypes · ${int(cascade.tick_count)} ticks`}
                  tone="accent2"
                  dot={false}
                />
              ) : undefined
            }
          >
            <form
              className="grid gap-2 md:grid-cols-3"
              onSubmit={(event) => {
                event.preventDefault();
                void runShock();
              }}
              aria-label="MiroFish shock parameters"
            >
              <label className="field">
                <span className="field-label">Symbol</span>
                <input
                  className="input"
                  value={shockSymbol}
                  onChange={(event) => setShockSymbol(event.target.value)}
                  aria-label="Shock symbol"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Silo</span>
                <select
                  className="select"
                  value={shockSilo}
                  onChange={(event) => setShockSilo(event.target.value)}
                  aria-label="Shock silo"
                >
                  {SILOS.map((silo) => (
                    <option key={silo} value={silo}>
                      {silo}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Sentiment tag</span>
                <input
                  className="input"
                  value={sentiment}
                  onChange={(event) => setSentiment(event.target.value)}
                  aria-label="Sentiment tag"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Shock (bps)</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={shockBpsValue}
                  onChange={(event) => setShockBpsValue(event.target.value)}
                  aria-label="Price shock in basis points"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Volatility ratio</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={volatilityRatio}
                  onChange={(event) => setVolatilityRatio(event.target.value)}
                  aria-label="Realised volatility ratio"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Spread (bps)</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={shockSpread}
                  onChange={(event) => setShockSpread(event.target.value)}
                  aria-label="Spread in basis points"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Gamma flip proximity</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={gammaProximity}
                  onChange={(event) => setGammaProximity(event.target.value)}
                  aria-label="Dealer gamma flip proximity"
                  autoComplete="off"
                />
              </label>
              <div className="flex items-end gap-2 md:col-span-2">
                <button
                  type="submit"
                  className="btn btn-accent"
                  disabled={shock.busy}
                  aria-label="Run the MiroFish contagion cascade"
                >
                  <Zap size={12} aria-hidden="true" />
                  {shock.busy ? "Simulating…" : "Run cascade"}
                </button>
                <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
                  SLA &lt; 1.8 ms
                </span>
              </div>
              {shockFormError ? (
                <p role="alert" className="mono text-[10.5px] text-[var(--neg)] md:col-span-3">
                  {shockFormError}
                </p>
              ) : null}
            </form>

            {shock.error ? (
              <div className="mt-2">
                <ErrorBlock message={shock.error} onRetry={() => void runShock()} />
              </div>
            ) : null}

            {cascade ? (
              <div className="mt-3 flex flex-col gap-3">
                <ContagionFlowGraph
                  archetypes={cascade.archetypes}
                  tickFlows={cascade.tick_flows}
                  matrix={cascade.tick_breakdown.map((breakdown) => breakdown.archetype_flows ?? [])}
                  weights={cascade.archetype_weights}
                  fragility={cascade.cascade_fragility_index}
                  threshold={cascade.fragility_halt_threshold}
                />
                <div className="grid-auto-180">
                  <MetricTile
                    label="Fragility index"
                    value={<TabularValue value={cascade.cascade_fragility_index} digits={3} />}
                    hint={
                      <span>
                        Ring 1 halts at{" "}
                        <TabularValue value={cascade.fragility_halt_threshold} digits={2} />
                      </span>
                    }
                    tone={
                      cascade.cascade_fragility_index >= cascade.fragility_halt_threshold ? "neg" : "pos"
                    }
                  />
                  <MetricTile
                    label="Crowd asymmetry"
                    value={<TabularValue value={cascade.crowd_asymmetry_score} digits={3} />}
                    hint="conviction skew across archetypes"
                    tone={cascade.crowd_asymmetry_score >= 0 ? "pos" : "neg"}
                  />
                  <MetricTile
                    label="Narrative exhaustion"
                    value={<TabularValue value={cascade.narrative_exhaustion_divergence} digits={3} />}
                    hint="divergence from crowd positioning"
                    tone={cascade.narrative_exhaustion_divergence >= 0 ? "pos" : "neg"}
                  />
                  <MetricTile
                    label="Cascade latency"
                    value={latency(cascade.latency_ms)}
                    hint={
                      <StatusPill
                        value={cascade.within_sla ? "WITHIN 1.8 ms SLA" : "SLA BREACH"}
                        tone={cascade.within_sla ? "ok" : "bad"}
                      />
                    }
                    tone={cascade.within_sla ? "pos" : "neg"}
                  />
                  <MetricTile
                    label="Revoked long leases"
                    value={<TabularValue value={cascade.revoked_long_leases} kind="int" />}
                    hint="on a fragility halt"
                    tone={cascade.revoked_long_leases > 0 ? "neg" : "dim"}
                  />
                  <MetricTile
                    label="Grounding digest"
                    value={<CopyHash value={cascade.sha256_digest} label="copy cascade digest" />}
                    hint="SHA-256 over the deterministic cascade"
                    mono={false}
                  />
                </div>
              </div>
            ) : (
              <div className="mt-3">
                <EmptyCleanSlate
                  title="No cascade simulated yet"
                  body="Each run pushes a signed shock through five behavioural archetypes for three ticks. The graph plots weighted flow per archetype, and every figure carries a SHA-256 commitment."
                />
              </div>
            )}
          </GlassPanel>

          <GlassPanel
            title="System 2 investment committee"
            eyebrow="bull · bear · risk · portfolio manager"
            actions={
              debate ? (
                <StatusPill
                  value={`${int(debate.stage_count)} stages`}
                  tone="accent"
                  dot={false}
                />
              ) : undefined
            }
          >
            <form
              className="grid gap-2 sm:grid-cols-3"
              onSubmit={(event) => {
                event.preventDefault();
                void runCommittee();
              }}
              aria-label="Committee debate parameters"
            >
              <label className="field">
                <span className="field-label">Silo</span>
                <select
                  className="select"
                  value={committeeSilo}
                  onChange={(event) => setCommitteeSilo(event.target.value)}
                  aria-label="Committee silo"
                >
                  {SILOS.map((silo) => (
                    <option key={silo} value={silo}>
                      {silo}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Symbol</span>
                <input
                  className="input"
                  value={committeeSymbol}
                  onChange={(event) => setCommitteeSymbol(event.target.value)}
                  aria-label="Committee symbol, or AUTO"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span className="field-label">Spread (bps)</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={committeeSpread}
                  onChange={(event) => setCommitteeSpread(event.target.value)}
                  aria-label="Committee spread in basis points"
                  autoComplete="off"
                />
              </label>
              <div className="flex items-end sm:col-span-3">
                <button
                  type="submit"
                  className="btn btn-accent"
                  disabled={committee.busy}
                  aria-label="Convene the System 2 investment committee"
                >
                  {committee.busy ? "Debating…" : "Convene committee"}
                </button>
              </div>
              {committeeFormError ? (
                <p role="alert" className="mono text-[10.5px] text-[var(--neg)] sm:col-span-3">
                  {committeeFormError}
                </p>
              ) : null}
            </form>

            {committee.error ? (
              <div className="mt-2">
                <ErrorBlock message={committee.error} onRetry={() => void runCommittee()} />
              </div>
            ) : null}

            {debate ? (
              <div className="mt-3 flex flex-col gap-2">
                <div className="row-between flex-wrap gap-2">
                  <div className="row flex-wrap gap-2">
                    <StatusPill
                      value={debate.verdict}
                      tone={debate.verdict === "CONVICTED" ? "ok" : "warn"}
                    />
                    <span className="mono tnum text-[10.5px] text-[var(--dim)]">
                      {debate.symbol} · rank {int(debate.rank)} · score {num(debate.score, 3)} ·{" "}
                      {price(debate.price)}
                    </span>
                  </div>
                  <StatusPill
                    value={`${num(debate.elapsed_ms, 0)} ms`}
                    tone={debate.elapsed_ms <= 15_000 ? "ok" : "warn"}
                    dot={false}
                    title="Committee SLA is 8–15 s p50/p99"
                  />
                </div>

                {debate.verdict === "ABSTAINED" ? (
                  <p className="rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-3 py-2 text-[11.5px] text-[var(--warn)]">
                    {text(debate.abstention_reason, "The committee abstained without a stated reason.")}
                  </p>
                ) : null}

                <ol className="flex flex-col" aria-label="Committee transcript">
                  {debate.transcript.map((turn, index) => (
                    <li key={`${turn.stage}-${index}`} className="relative pl-6 pb-4">
                      <span
                        className="mono absolute top-0 left-0 flex h-4 w-4 items-center justify-center rounded-full border border-[var(--line-strong)] bg-[var(--panel-solid)] text-[9px] text-[var(--dim)]"
                        aria-hidden="true"
                      >
                        {index + 1}
                      </span>
                      {index < debate.transcript.length - 1 ? (
                        <span
                          className="absolute top-5 bottom-0 left-[7px] w-px bg-[var(--line)]"
                          aria-hidden="true"
                        />
                      ) : null}
                      <div className="row-between flex-wrap gap-1.5">
                        <p className="eyebrow">{titleize(turn.stage)}</p>
                        <StatusPill
                          value={turn.verified ? "VERIFIED" : "UNVERIFIED"}
                          tone={passTone(turn.verified)}
                        />
                      </div>
                      <p className="mono text-[9.5px] tracking-[0.06em] text-[var(--faint)] uppercase">
                        {turn.actor}
                      </p>
                      <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-[var(--ink-soft)]">
                        {turn.text}
                      </p>
                      <p className="mono mt-1 text-[9.5px] text-[var(--faint)]">
                        <TabularValue value={turn.claims_checked} kind="int" /> claims checked
                      </p>
                    </li>
                  ))}
                </ol>

                <div className="row-between border-t border-[var(--line)] pt-2">
                  <span className="eyebrow">packet digest</span>
                  <CopyHash value={debate.sha256_digest} label="copy committee packet digest" />
                </div>
              </div>
            ) : (
              <div className="mt-3">
                <EmptyCleanSlate
                  title="No committee convened yet"
                  body="Four sealed turns run here: both analysts, the bull and bear researchers, the three-way risk debate, then the portfolio manager's verdict. Any unhashed number aborts the packet."
                />
              </div>
            )}
          </GlassPanel>
        </div>
      </div>
    </>
  );
}
