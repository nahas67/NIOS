/**
 * `/verification` — TLA+ state-space proofs, VOPR fault campaign, SIGKILL drill,
 * cold-start reconciliation, clock health and Prometheus telemetry.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  Checkbox,
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
import { SILOS } from "../config/nav";
import { api, errorMessage } from "../lib/api";
import { asArray, asRecord, clockNs, duration, int, num, safeNumber, text } from "../lib/format";
import { numberAt, rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const EXPECTED_MODELS = ["KillSwitch", "OrderLifecycle", "Outbox", "Reconciliation"];
const EXPECTED_STATES = 35_771;
const DRILLS = [{ value: "SIGKILL", label: "SIGKILL · outbox reservation" }];
const MAX_METRICS = 60;

interface MetricRow {
  metric: string;
  value: string;
}

function flattenMetrics(value: unknown, prefix: string, out: MetricRow[]): void {
  if (out.length >= MAX_METRICS) return;
  if (typeof value === "number") {
    out.push({ metric: prefix, value: Number.isInteger(value) ? int(value) : num(value, 4) });
    return;
  }
  if (typeof value === "boolean") {
    out.push({ metric: prefix, value: value ? "true" : "false" });
    return;
  }
  if (Array.isArray(value)) {
    const head = value.slice(0, 12);
    for (let index = 0; index < head.length; index += 1) {
      flattenMetrics(head[index], `${prefix}[${index}]`, out);
    }
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
      flattenMetrics(inner, prefix.length > 0 ? `${prefix}.${key}` : key, out);
    }
  }
}

export function VerificationPage({ mode }: PageProps) {
  const [proofs, setProofs] = useState<Record<string, unknown>>({});
  const [reconciliation, setReconciliation] = useState<Record<string, unknown>>({});
  const [clock, setClock] = useState<Record<string, unknown>>({});
  const [metrics, setMetrics] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [vopr, setVopr] = useState<Record<string, unknown> | null>(null);
  const [drill, setDrill] = useState<Record<string, unknown> | null>(null);
  const [osTune, setOsTune] = useState<Record<string, unknown> | null>(null);
  const [runResult, setRunResult] = useState<Record<string, unknown> | null>(null);
  const [drillKind, setDrillKind] = useState("SIGKILL");
  const [clearSilo, setClearSilo] = useState("");
  const [clearReason, setClearReason] = useState("");
  const [freezeConfirmed, setFreezeConfirmed] = useState(false);
  const [lastSync, setLastSync] = useState(0);
  const now = useTicker(1000);

  const load = useCallback(async () => {
    try {
      const [proofPayload, reconPayload, clockPayload, metricPayload] = await Promise.all([
        api.get<unknown>("/cockpit/formal-proofs"),
        api.get<unknown>("/reconciliation/status"),
        api.get<unknown>("/cockpit/clock"),
        api.get<unknown>("/metrics/json"),
      ]);
      setProofs(asRecord(proofPayload));
      setReconciliation(asRecord(reconPayload));
      setClock(asRecord(clockPayload));
      setMetrics(asRecord(metricPayload));
      setLastSync(Date.now());
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 10_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const summary = useMemo(() => asRecord(proofs.summary), [proofs]);
  const tla = useMemo(() => asRecord(proofs.tla), [proofs]);
  const specs = useMemo(() => rowsOf(tla.specs, ["specs"]), [tla]);
  const statesExplored = numberAt(summary, ["states_explored"], 0);
  const counterexamples = numberAt(summary, ["counterexamples"], 0);
  const proofsOk = summary.tla_ok === true && counterexamples === 0;

  const frozenList = useMemo(() => {
    const raw = reconciliation.frozen_silos;
    return Array.isArray(raw) ? raw.map((entry) => text(entry, "")).filter(Boolean) : [];
  }, [reconciliation]);
  const lastReport = useMemo(() => asRecord(reconciliation.last_report), [reconciliation]);
  const reportRows = useMemo(() => rowsOf(lastReport.rows, ["rows"]), [lastReport]);
  const driftBps = useMemo(() => {
    let worst = 0;
    for (const row of reportRows) {
      const internal = Math.abs(numberAt(row, ["internal_qty_micros"], 0));
      const delta = Math.abs(numberAt(row, ["delta_micros"], 0));
      const ratio = internal > 0 ? (delta / internal) * 10_000 : delta > 0 ? 10_000 : 0;
      if (ratio > worst) worst = ratio;
    }
    return worst;
  }, [reportRows]);

  const telemetry = useMemo(() => {
    const rows: MetricRow[] = [];
    flattenMetrics(metrics, "", rows);
    return rows.slice(0, MAX_METRICS);
  }, [metrics]);

  const selectedFrozen = clearSilo || frozenList[0] || SILOS[0].id;
  const canClearFreeze =
    mode === "LIVE" && freezeConfirmed && clearReason.trim().length >= 3 && busy === null;

  const runVopr = async () => {
    setNote(null);
    setBusy("vopr");
    try {
      const payload = asRecord(
        await api.post<unknown>("/control/verification/vopr", { campaign: "standard", seed: 7, steps: 250 }),
      );
      setVopr(payload);
      setNote({
        tone: payload.status === "PASSED" ? "bull" : "bear",
        message: `VOPR ${text(payload.status, "UNKNOWN")} · ${int(payload.injected_faults)} faults injected · ${int(
          payload.overdrafts,
        )} overdrafts · ${int(payload.duplicate_executions)} duplicate executions`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const runDrill = async () => {
    setNote(null);
    setBusy("drill");
    try {
      const payload = asRecord(
        await api.post<unknown>("/control/verification/chaos-drill", {
          drill: drillKind,
          target: "outbox_reservation",
          iterations: 120,
          silo: "CRYPTO_BINANCE",
        }),
      );
      setDrill(payload);
      setNote({
        tone: payload.status === "PASSED" ? "bull" : "bear",
        message: `${drillKind} DRILL ${text(payload.status, "UNKNOWN")} · killed=${text(
          payload.killed,
        )} · all invariants hold=${text(payload.all_invariants_hold)}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const runOsTune = async () => {
    setNote(null);
    setBusy("os-tune");
    try {
      const payload = asRecord(
        await api.post<unknown>("/control/verification/os-tune", { apply: true, ring: "ring1" }),
      );
      setOsTune(payload);
      setNote({
        tone: payload.status === "PASSED" ? "bull" : "bear",
        message: `OS TUNE ${text(payload.status, "UNKNOWN")} · ${int(payload.applied_count)} applied · ${int(
          payload.recommended_count,
        )} recommended · profile ${text(payload.profile, "—")}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const runReconciliation = async () => {
    setNote(null);
    setBusy("reconcile");
    try {
      const payload = asRecord(await api.post<unknown>("/control/reconciliation/run", { dry_run: false }));
      setRunResult(payload);
      const frozen = Array.isArray(payload.frozen_silos) ? payload.frozen_silos.length : 0;
      setNote({
        tone: payload.ran === true ? "bull" : "neutral",
        message:
          payload.ran === true
            ? `RECONCILIATION RAN · frozen ${int(frozen)} silo(s) · within deadline ${text(payload.within_deadline, "—")}`
            : `RECONCILIATION SKIPPED · ${text(payload.reason, "NO_RECONCILER_ATTACHED")}`,
      });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const clearFreeze = async () => {
    setNote(null);
    setBusy("clear-freeze");
    try {
      if (mode !== "LIVE") throw new Error("LIVE-GUARDED — PAPER REFUSES CLEAR-FREEZE");
      if (!freezeConfirmed) throw new Error("clear-freeze requires the confirmation checkbox");
      if (clearReason.trim().length < 3) throw new Error("clear-freeze requires an operator note (3+ chars)");
      const payload = asRecord(
        await api.post<unknown>("/control/reconciliation/clear-freeze", {
          silo: selectedFrozen,
          note: clearReason.trim(),
          operator: "ring3.operator",
        }),
      );
      setNote({
        tone: payload.cleared === true ? "bull" : "neutral",
        message: `CLEAR-FREEZE ${text(payload.silo, selectedFrozen)} · cleared=${text(
          payload.cleared,
        )} · previous ${text(payload.previous_reason, "—")}`,
      });
      setFreezeConfirmed(false);
      setClearReason("");
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
        title="VERIFICATION"
        subtitle="Exhaustive TLA+ BFS · VOPR fault injection · SIGKILL recovery · cold-start reconciliation"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"}>{mode}</Badge>
            <Badge tone={proofsOk ? "bull" : "bear"}>{proofsOk ? "PROVEN" : "UNPROVEN"}</Badge>
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
        <Stat
          label="States Explored"
          value={int(statesExplored)}
          hint={statesExplored === EXPECTED_STATES ? "== 35,771 release baseline" : "release baseline 35,771"}
          tone={statesExplored === EXPECTED_STATES ? "bull" : "accent"}
        />
        <Stat label="Models" value={int(specs.length)} hint="4 TLA+ specifications expected" />
        <Stat
          label="Counterexamples"
          value={int(counterexamples)}
          tone={counterexamples === 0 ? "bull" : "bear"}
        />
        <Stat
          label="Frozen Silos"
          value={int(frozenList.length)}
          tone={frozenList.length === 0 ? "bull" : "bear"}
          hint="cold-start reconciliation"
        />
        <Stat
          label="Clock Offset"
          value={`${num(clock.offset_ms, 3)} ms`}
          tone={clock.ok === false ? "bear" : "bull"}
          hint={`budget ${num(clock.budget_ms, 1)} ms`}
        />
      </StatGrid>

      <Card
        title="TLA+ State-Space Proofs"
        subtitle="Exhaustive breadth-first reachability · finite transition systems · zero counterexamples required"
        icon="verification"
        dense
        actions={
          <Badge tone={proofsOk ? "bull" : "bear"} mono>
            {int(tla.total_states ?? statesExplored)} STATES
          </Badge>
        }
      >
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> EXPLORING STATE SPACE…
          </div>
        ) : (
          <Table
            dense
            columns={["MODEL", "STATES", "TRANSITIONS", "INVARIANTS", "COUNTEREXAMPLES", "RESULT"]}
          >
            {EXPECTED_MODELS.map((model) => {
              const row = specs.find((entry) => text(entry.name, "") === model);
              const invariants = row
                ? asArray(row.invariants)
                    .map((entry) => text(entry, ""))
                    .filter(Boolean)
                : [];
              const ok = row ? row.ok === true : false;
              return (
                <Row key={model} tone={row && !ok ? "bear" : undefined}>
                  <Cell className="font-semibold text-ink">{model}</Cell>
                  <Cell align="right">{row ? int(row.states_explored) : "—"}</Cell>
                  <Cell align="right" tone="muted">
                    {row ? int(row.transitions) : "—"}
                  </Cell>
                  <Cell className="text-[10px] text-ink-2">
                    {invariants.length === 0
                      ? "—"
                      : `${invariants.slice(0, 3).join(" · ")}${invariants.length > 3 ? ` +${invariants.length - 3}` : ""}`}
                  </Cell>
                  <Cell
                    align="right"
                    tone={row && numberAt(row, ["counterexamples"], 0) > 0 ? "bear" : "bull"}
                  >
                    {row ? int(row.counterexamples) : "—"}
                  </Cell>
                  <Cell align="center">
                    <Badge tone={!row ? "muted" : ok ? "bull" : "bear"}>
                      {!row ? "NOT REPORTED" : ok ? "PASS" : "FAIL"}
                    </Badge>
                  </Cell>
                </Row>
              );
            })}
            <Row>
              <Cell colSpan={6} tone="muted" className="text-[10px]">
                headline · {int(statesExplored)} states explored across {int(specs.length)} specifications ·{" "}
                {text(tla.mode, "exhaustive_bfs")} · suite duration {num(tla.duration_ms, 1)} ms
              </Cell>
            </Row>
          </Table>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="VOPR Fault Campaign"
          subtitle="Seeded deterministic fault injection over the real Ring 0 ledger stack"
          icon="flame"
          actions={
            <Button
              variant="primary"
              size="sm"
              icon="play"
              disabled={busy !== null}
              onClick={() => void runVopr()}
            >
              {busy === "vopr" ? "RUNNING…" : "Run VOPR"}
            </Button>
          }
        >
          {vopr === null ? (
            <EmptyState
              title="NO CAMPAIGN IN THIS SESSION"
              hint="Seed 7 · 250 steps · network drop, duplicate NATS, overdraft attempt, power loss."
            />
          ) : (
            <>
              <div className="mb-2 flex items-center gap-2">
                <Badge tone={vopr.status === "PASSED" ? "bull" : "bear"}>
                  {text(vopr.status, "UNKNOWN")}
                </Badge>
                <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                  seed {int(vopr.seed)} · {int(vopr.steps)} steps · {num(vopr.duration_ms, 1)} ms
                </span>
              </div>
              <KeyValue
                columns={3}
                items={[
                  { label: "Faults Injected", value: int(vopr.injected_faults) },
                  {
                    label: "Overdrafts",
                    value: int(vopr.overdrafts),
                    tone: vopr.overdrafts === 0 ? "bull" : "bear",
                  },
                  {
                    label: "Conservation",
                    value: int(vopr.conservation_violations),
                    tone: vopr.conservation_violations === 0 ? "bull" : "bear",
                  },
                  { label: "Ledger Violations", value: int(vopr.ledger_violations), tone: "bear" },
                  { label: "Duplicates", value: int(vopr.duplicate_executions), tone: "bear" },
                  { label: "Power Cycles", value: int(vopr.power_cycles) },
                  { label: "Reservations", value: int(vopr.reservations) },
                  { label: "Commits", value: int(vopr.commits) },
                  { label: "Voids", value: int(vopr.voids) },
                ]}
              />
              <div className="mt-2 flex flex-wrap gap-1">
                {Object.entries(asRecord(vopr.fault_counts)).map(([key, value]) => (
                  <Badge key={key} tone={safeNumber(value, 0) > 0 ? "accent" : "muted"} mono>
                    {key} {int(value)}
                  </Badge>
                ))}
              </div>
            </>
          )}
        </Card>

        <Card
          title="Chaos Drill · SIGKILL"
          subtitle="Worker killed mid-outbox · journal replay must restore every reboot invariant"
          icon="zap"
          actions={
            <Button
              variant="primary"
              size="sm"
              icon="power"
              disabled={busy !== null}
              onClick={() => void runDrill()}
            >
              {busy === "drill" ? "KILLING…" : "Run Drill"}
            </Button>
          }
        >
          <Field label="Drill" className="mb-2 w-64">
            <SelectInput value={drillKind} onChange={setDrillKind} options={DRILLS} />
          </Field>
          {drill === null ? (
            <EmptyState
              title="NO DRILL IN THIS SESSION"
              hint="The drill SIGKILLs a worker holding an outbox reservation and replays the journal."
            />
          ) : (
            <>
              <div className="mb-2 flex items-center gap-2">
                <Badge tone={drill.status === "PASSED" ? "bull" : "bear"}>
                  {text(drill.status, "UNKNOWN")}
                </Badge>
                <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                  pid {int(drill.pid)} · return {int(drill.returncode)} · {num(drill.elapsed_ms, 1)} ms
                </span>
              </div>
              <Table dense columns={["INVARIANT", "HOLDS"]}>
                {Object.entries(asRecord(drill.invariants)).map(([key, value]) => (
                  <Row key={key}>
                    <Cell className="font-mono text-[10px]">{key}</Cell>
                    <Cell align="right">
                      <Badge tone={value === true ? "bull" : "bear"}>
                        {value === true ? "HOLDS" : "VIOLATED"}
                      </Badge>
                    </Cell>
                  </Row>
                ))}
              </Table>
            </>
          )}
        </Card>
      </div>

      <Card
        title="ColdStartReconciler"
        subtitle="Broker snapshot vs IBOR · unexplained difference freezes the silo, not the account"
        icon="database"
        actions={
          <Badge tone={frozenList.length === 0 ? "bull" : "bear"}>
            {frozenList.length === 0 ? "NO FREEZE" : `${int(frozenList.length)} FROZEN`}
          </Badge>
        }
      >
        <div className="grid gap-3 xl:grid-cols-2">
          <div>
            <KeyValue
              columns={2}
              items={[
                {
                  label: "Freeze State",
                  value: frozenList.length === 0 ? "CLEAR" : frozenList.join(" · "),
                  tone: frozenList.length === 0 ? "bull" : "bear",
                },
                {
                  label: "Drift (bps, qty-weighted)",
                  value: `${num(driftBps, 2)} bps`,
                  tone: driftBps > 100 ? "bear" : "bull",
                },
                {
                  label: "Pending Intents",
                  value: int(numberAt(lastReport.counts, ["total"], reportRows.length)),
                },
                { label: "Last Run", value: clockNs(lastReport.finished_at_ns) },
                {
                  label: "Passed",
                  value: text(lastReport.passed, "—"),
                  tone: lastReport.passed === true ? "bull" : "bear",
                },
                {
                  label: "Reconciler",
                  value: reconciliation.reconciler_attached === true ? "ATTACHED" : "NOT ATTACHED",
                },
              ]}
            />
            <div className="mt-3 flex flex-wrap items-end gap-2">
              <Field label="Freeze Silo" className="w-56">
                <SelectInput
                  value={selectedFrozen}
                  onChange={setClearSilo}
                  options={(frozenList.length > 0 ? frozenList : SILOS.map((silo) => silo.id)).map((id) => ({
                    value: id,
                    label: id,
                  }))}
                />
              </Field>
              <Field label="Operator Note" className="w-64" hint="3+ chars · recorded in the alert journal">
                <TextInput value={clearReason} onChange={(event) => setClearReason(event.target.value)} />
              </Field>
              <Button
                variant="subtle"
                icon="refresh"
                disabled={busy !== null}
                onClick={() => void runReconciliation()}
              >
                {busy === "reconcile" ? "RUNNING…" : "Run Reconciliation"}
              </Button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-hairline pt-2">
              <Checkbox
                checked={freezeConfirmed}
                onChange={setFreezeConfirmed}
                label="I confirm this freeze is a reconciliation freeze, cleared by an operator note."
              />
              <Button
                variant="danger"
                icon="unlock"
                disabled={!canClearFreeze}
                onClick={() => void clearFreeze()}
              >
                {busy === "clear-freeze" ? "CLEARING…" : "Clear Freeze"}
              </Button>
            </div>
            {mode === "LIVE" ? null : (
              <div className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
                LIVE-GUARDED — PAPER REFUSES CLEAR-FREEZE
              </div>
            )}
          </div>
          <div>
            {reportRows.length === 0 ? (
              <EmptyState
                title="NO RECONCILIATION REPORT"
                hint="Run the reconciler to classify every broker snapshot against the IBOR."
              />
            ) : (
              <Table dense columns={["SILO", "SYMBOL", "INTERNAL", "BROKER", "DELTA", "CLASS"]}>
                {reportRows.slice(0, 10).map((row, index) => (
                  <Row
                    key={`${text(row.symbol, "row")}-${index}`}
                    tone={numberAt(row, ["delta_micros"], 0) === 0 ? undefined : "bear"}
                  >
                    <Cell className="text-[10px]">{text(row.silo)}</Cell>
                    <Cell mono>{text(row.symbol)}</Cell>
                    <Cell align="right">{int(row.internal_qty_micros)}</Cell>
                    <Cell align="right">{int(row.broker_qty_micros)}</Cell>
                    <Cell align="right" tone={numberAt(row, ["delta_micros"], 0) === 0 ? "bull" : "bear"}>
                      {int(row.delta_micros)}
                    </Cell>
                    <Cell align="right" tone="muted" className="text-[10px]">
                      {text(row.classification)}
                    </Cell>
                  </Row>
                ))}
              </Table>
            )}
            {runResult ? (
              <div className="mt-2 text-[10px] text-ink-3">
                last run · {text(runResult.page, "reconciliation.run")}
              </div>
            ) : null}
          </div>
        </div>
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="Clock Health Audit"
          subtitle="Wall clock vs monotonic anchor · Ring 0 fails closed beyond the budget"
          icon="clock"
        >
          <KeyValue
            columns={2}
            items={[
              {
                label: "Monotonic",
                value:
                  safeNumber(clock.monotonic_ns, 0) > 0
                    ? duration(safeNumber(clock.monotonic_ns, 0) / 1e9)
                    : "—",
              },
              { label: "Wall UTC", value: clockNs(clock.wall_clock_ns) },
              {
                label: "NTP Offset",
                value: `${num(clock.offset_ms, 3)} ms`,
                tone: safeNumber(clock.offset_ms, 0) === 0 ? "bull" : "bear",
              },
              {
                label: "Drift",
                value: `${num(clock.offset_ms, 3)} / ${num(clock.budget_ms, 1)} ms`,
                tone: clock.ok === false ? "bear" : "bull",
              },
            ]}
          />
          <div className="mt-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
            <Badge tone={clock.ok === false ? "bear" : "bull"}>
              {clock.ok === false ? "DRIFT OVER BUDGET" : "WITHIN BUDGET"}
            </Badge>
            source {text(clock.source, "—")} · synthetic {text(clock.synthetic, "—")}
          </div>
        </Card>

        <Card
          title="OS Low-Latency Tune"
          subtitle="Socket, timer, affinity, shared-memory and sysctl audit"
          icon="server"
          actions={
            <Button
              variant="subtle"
              size="sm"
              icon="zap"
              disabled={busy !== null}
              onClick={() => void runOsTune()}
            >
              {busy === "os-tune" ? "PROBING…" : "OS Tune Probe"}
            </Button>
          }
        >
          {osTune === null ? (
            <EmptyState
              title="NOT PROBED IN THIS SESSION"
              hint="The probe applies only what the serving process is permitted to change."
            />
          ) : (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge tone={osTune.status === "PASSED" ? "bull" : "bear"}>
                  {text(osTune.status, "UNKNOWN")}
                </Badge>
                <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                  {text(osTune.platform, "—")} · {text(osTune.profile, "—")} · ring {text(osTune.ring, "—")}
                </span>
              </div>
              <KeyValue
                columns={3}
                items={[
                  { label: "Applied", value: int(osTune.applied_count), tone: "bull" },
                  { label: "Recommended", value: int(osTune.recommended_count), tone: "accent" },
                  { label: "Tuned", value: text(osTune.tuned, "—") },
                ]}
              />
              <div className="mt-2 flex flex-wrap gap-1">
                {rowsOf(asRecord(osTune.groups).socket, ["socket"])
                  .slice(0, 6)
                  .map((row, index) => (
                    <Badge
                      key={`${text(row.id, "check")}-${index}`}
                      tone={text(row.status, "").toUpperCase() === "APPLIED" ? "bull" : "muted"}
                      mono
                    >
                      {text(row.id, "check")} {text(row.status, "")}
                    </Badge>
                  ))}
              </div>
            </>
          )}
        </Card>
      </div>

      <Card
        title="Prometheus Telemetry"
        subtitle={`/metrics exposes the text exposition · /metrics/json drives this table · capped at ${MAX_METRICS} rows`}
        icon="activity"
        dense
        actions={
          <Badge tone="muted" mono>
            {int(telemetry.length)} METRICS
          </Badge>
        }
      >
        {telemetry.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO TELEMETRY"
              hint="The exporter returned no numeric families. Check /metrics on the gateway."
            />
          </div>
        ) : (
          <Table dense columns={["METRIC", "VALUE"]}>
            {telemetry.map((row, index) => (
              <Row key={`${row.metric}-${index}`}>
                <Cell mono className="text-[10px] text-ink-2">
                  {row.metric}
                </Cell>
                <Cell align="right">{row.value}</Cell>
              </Row>
            ))}
          </Table>
        )}
        <div className="border-t border-hairline px-2.5 py-1.5 text-[10px] text-ink-3">
          text endpoint · <span className="font-mono text-ink-2">GET /metrics</span> · alias{" "}
          <span className="font-mono text-ink-2">/api/v2/metrics</span> · json{" "}
          <span className="font-mono text-ink-2">/api/v2/metrics/json</span>
        </div>
      </Card>

      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
        <span>poll 10s</span>
        <span>·</span>
        <span>last sync {clockNs(lastSync * 1e6)} UTC</span>
        <span>·</span>
        <span>now {clockNs(now * 1e6)} UTC</span>
      </div>
    </div>
  );
}

export default VerificationPage;
