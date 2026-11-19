import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  StatusDot,
  Table,
  TextInput,
  Toolbar,
} from "../components/ui";
import { RING_LEGEND, SILOS } from "../config/nav";
import { api, assertFinite, errorMessage } from "../lib/api";
import {
  asArray,
  asRecord,
  duration,
  int,
  num,
  safeNumber,
  shortHash,
  signed,
  text,
  upper,
} from "../lib/format";
import { rowsOf } from "../lib/payload";
import { mergeTelemetry, telemetryOf, useGatewayTelemetry } from "../lib/telemetry";
import { GatewayHealthStats, RingP99Stats } from "../components/telemetry";
import { useTicker } from "../state/session";
import type { PageProps } from "./types";

type Note = { tone: "neutral" | "bull" | "bear"; message: string };
type Stance = "BULL" | "BEAR" | "NEUTRAL";

const SILO_OPTIONS = SILOS.map((silo) => ({
  value: silo.id,
  label: `${silo.short} · ${silo.floorBps.toFixed(2)} bps`,
}));
const LADDER_FALLBACK = [1, 5, 10, 30, 60];
const COMMITTEE_STAGES: Array<{ id: string; label: string; match: (stage: string) => boolean }> = [
  { id: "analysts", label: "ANALYSTS", match: (stage) => stage.startsWith("analyst") },
  { id: "researchers", label: "RESEARCHERS", match: (stage) => stage.startsWith("debate") },
  { id: "risk", label: "RISK DEBATE", match: (stage) => stage.includes("risk") },
  { id: "trader", label: "TRADER", match: (stage) => stage === "trader" },
];

function ladderLabel(seconds: number): string {
  return seconds >= 60 ? `${Math.round(seconds / 60)}m` : `${seconds}s`;
}

function stanceOf(actor: unknown, stage: unknown, textValue: unknown): Stance {
  const token = `${text(actor, "")} ${text(stage, "")} ${text(textValue, "")}`.toUpperCase();
  if (token.includes("BEAR") || token.includes("SHORT") || token.includes("SELL")) return "BEAR";
  if (token.includes("BULL") || token.includes("LONG") || token.includes("BUY")) return "BULL";
  return "NEUTRAL";
}

function stanceTone(stance: Stance): "bull" | "bear" | "neutral" {
  if (stance === "BULL") return "bull";
  if (stance === "BEAR") return "bear";
  return "neutral";
}

function stampNs(value: unknown): string {
  const ns = safeNumber(value, Number.NaN);
  if (!Number.isFinite(ns) || ns <= 0) return "—";
  return new Date(ns / 1e6).toISOString().replace("T", " ").slice(0, 19);
}

export function BrainPage({ mode }: PageProps) {
  const [state, setState] = useState<Record<string, unknown>>({});
  const gatewayTelemetry = useGatewayTelemetry();
  const [lifecycle, setLifecycle] = useState<Record<string, unknown>>({});
  const [seed] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const [shockSymbol, setShockSymbol] = useState("BTCUSDT");
  const [shockBps, setShockBps] = useState("-45");
  const [shockSilo, setShockSilo] = useState("CRYPTO_BINANCE");
  const [shock, setShock] = useState<Record<string, unknown> | null>(null);

  const [debateSymbol, setDebateSymbol] = useState("AUTO");
  const [debateSilo, setDebateSilo] = useState("US_IBKR");
  const [debateQuestion, setDebateQuestion] = useState(
    "Is the dislocation worth the tail risk after statutory costs?",
  );
  const [debate, setDebate] = useState<Record<string, unknown> | null>(null);

  const now = useTicker(1000);

  const load = useCallback(async () => {
    const [stateResult, lifecycleResult] = await Promise.allSettled([
      api.get<unknown>("/cockpit/state"),
      api.get<unknown>("/lifecycle/status"),
    ]);
    if (stateResult.status === "fulfilled") {
      setState(asRecord(stateResult.value));
      setError(null);
    } else {
      setError(errorMessage(stateResult.reason));
    }
    if (lifecycleResult.status === "fulfilled") setLifecycle(asRecord(lifecycleResult.value));
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 8_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const supervisor = asRecord(state.supervisor);
  const telemetry = useMemo(
    () => mergeTelemetry(telemetryOf(state), gatewayTelemetry),
    [state, gatewayTelemetry],
  );
  const ringRows = useMemo(() => {
    const primary = rowsOf(supervisor, ["rings"]);
    const fromState = primary.length > 0 ? primary : rowsOf(asRecord(state.ring_processes), ["rings"]);
    const fallback: Record<string, unknown>[] = [0, 1, 2, 3].map((index) => ({ index }));
    const source: Record<string, unknown>[] = fromState.length > 0 ? fromState : fallback;
    return source.slice(0, 4).map((row) => {
      const index = Math.max(0, Math.min(3, Math.trunc(safeNumber(row.index, 0))));
      const legend = RING_LEGEND[index];
      const alive = row.alive === true;
      const ageMs = safeNumber(row.heartbeat_age_ms, Number.NaN);
      const p99 = safeNumber(row.latency_p99_ms ?? row.p99_ms ?? row.latency_p99, Number.NaN);
      return {
        index,
        name: `${legend.ring} ${legend.label.toUpperCase()}`,
        worker: text(row.worker, "—"),
        pid: row.pid === null || row.pid === undefined ? "—" : int(row.pid),
        age: Number.isFinite(ageMs) ? `${num(ageMs, 1)} ms` : "—",
        p99: Number.isFinite(p99) ? `${num(p99, 3)} ms` : "—",
        tone: alive ? ("bull" as const) : fromState.length > 0 ? ("bear" as const) : ("muted" as const),
        state:
          fromState.length === 0
            ? "NOT ATTACHED"
            : alive
              ? "RUNNING"
              : row.spawned === true
                ? "DEAD"
                : "SPAWNED",
        restarts: int(row.restarts),
      };
    });
  }, [supervisor, state]);

  const ringsAlive = ringRows.filter((row) => row.tone === "bull").length;
  const ringsAttached = ringRows.some((row) => row.state !== "NOT ATTACHED");

  const unknownOrders = asRecord(lifecycle.unknown_orders);
  const listenKey = asRecord(lifecycle.listen_key);
  const ladder = useMemo(() => {
    const raw = asArray(unknownOrders.delays_s)
      .map((value) => safeNumber(value, 0))
      .filter((value) => value > 0);
    return raw.length > 0 ? raw : LADDER_FALLBACK;
  }, [unknownOrders]);
  const openUnknown = safeNumber(unknownOrders.count, 0);
  const escalated = safeNumber(unknownOrders.escalated, 0);
  const activeRung = escalated > 0 ? ladder.length - 1 : openUnknown > 0 ? 0 : -1;

  const totpSeconds = useMemo(() => {
    const stamp = new Date(now);
    const secondsOfDay = stamp.getUTCHours() * 3600 + stamp.getUTCMinutes() * 60 + stamp.getUTCSeconds();
    const delta = 3 * 3600 - secondsOfDay;
    return delta > 0 ? delta : delta + 86_400;
  }, [now]);
  const listenKeyExpiry = safeNumber(listenKey.expires_at_utc_ns, 0);
  const listenKeyRemaining =
    listenKeyExpiry > 0 ? Math.max(0, (listenKeyExpiry - now * 1e6) / 1e9) : Number.NaN;

  const fragility = safeNumber(shock?.cascade_fragility_index, Number.NaN);
  const shockLatency = safeNumber(shock?.latency_ms, Number.NaN);
  const withinSla = shock?.within_sla === true;

  const archetypeImpacts = useMemo(() => {
    const totals = new Map<string, number>();
    for (const tick of rowsOf(shock, ["tick_breakdown"])) {
      for (const row of rowsOf(tick, ["archetypes"])) {
        const name = text(row.name, "ARCHETYPE");
        totals.set(name, (totals.get(name) ?? 0) + safeNumber(row.flow, 0));
      }
    }
    const rows = Array.from(totals.entries()).map(([name, flow]) => ({ name, flow }));
    const peak = Math.max(1e-9, ...rows.map((row) => Math.abs(row.flow)));
    return rows.map((row) => ({ ...row, peak }));
  }, [shock]);

  const transcript = useMemo(() => rowsOf(debate ?? seed, ["transcript"]), [debate, seed]);
  const debateVerdict = upper(debate?.verdict ?? seed.status);
  const report = asRecord(debate?.report);
  const proposal = asRecord(debate?.proposal);

  const runShock = useCallback(async () => {
    setBusy(true);
    setNote(null);
    try {
      const shockValue = assertFinite(shockBps, "shock bps");
      if (shockValue < -5000 || shockValue > 5000) throw new Error("shock_bps must lie in [-5000, 5000]");
      const symbolValue = upper(shockSymbol.trim());
      if (!symbolValue) throw new Error("a symbol is required");
      const payload = await api.post<unknown>("/control/brain/mirofish-shock", {
        symbol: symbolValue,
        shock_bps: shockValue,
        silo: shockSilo,
        archetypes: 5,
        ticks: 3,
      });
      setShock(asRecord(payload));
      setNote({
        tone: "neutral",
        message: `CASCADE COMPLETE · ${symbolValue} · ${text(asRecord(payload).status, "—")}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [shockBps, shockSilo, shockSymbol]);

  const runCommittee = useCallback(async () => {
    setBusy(true);
    setNote(null);
    try {
      const symbolValue = upper(debateSymbol.trim()) || "AUTO";
      const payload = await api.post<unknown>("/control/brain/committee-debate", {
        symbol: symbolValue,
        silo: debateSilo,
        question: debateQuestion,
      });
      setDebate(asRecord(payload));
      setNote({
        tone: "neutral",
        message: `COMMITTEE ${upper(asRecord(payload).verdict)} · ${text(asRecord(payload).symbol, symbolValue)}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [debateQuestion, debateSilo, debateSymbol]);

  const forceTick = useCallback(async () => {
    setBusy(true);
    try {
      const payload = await api.post<unknown>("/control/lifecycle/tick", {});
      const record = asRecord(payload);
      setNote({ tone: "bull", message: `LIFECYCLE TICK · ${int(record.jobs_run)} JOBS RUN` });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [load]);

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="BRAIN"
        subtitle="4-ring supervisor, NiosLifecycleWorker clocks, MiroFish contagion and the System 2 committee"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone={ringsAlive === 4 ? "bull" : "muted"} mono>
              {ringsAlive}/4 RINGS
            </Badge>
            <Badge tone="neutral" mono>
              RING 1 SPINAL p99 &lt;0.05 ms
            </Badge>
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

      <StatGrid columns={4}>
        <Stat
          label="Rings alive"
          value={`${ringsAlive}/4`}
          hint={text(supervisor.reason ?? state.version, "four separate OS processes")}
          tone={ringsAlive === 4 ? "bull" : ringsAttached ? "bear" : "muted"}
        />
        <Stat label="TOTP renewal" value={duration(totpSeconds)} hint="next 03:00 UTC window" />
        <Stat
          label="Cascade fragility"
          value={Number.isFinite(fragility) ? num(fragility, 3) : "—"}
          hint="halt threshold 0.75"
          tone={Number.isFinite(fragility) ? (fragility >= 0.75 ? "bear" : "bull") : "muted"}
        />
        <Stat
          label="Committee"
          value={debateVerdict}
          hint={`${transcript.length} stages`}
          tone={debateVerdict === "CONVICTED" ? "bull" : debateVerdict === "ABSTAINED" ? "accent" : "muted"}
        />
      </StatGrid>

      <Card
        title="4-ring supervisor"
        subtitle={`GET /cockpit/state · ${text(supervisor.heartbeat_shm, "heartbeat shm not attached")}`}
        icon="server"
        actions={
          <Badge tone={supervisor.four_ring_isolation === true ? "bull" : "muted"} mono>
            {supervisor.four_ring_isolation === true ? "ISOLATED" : "SINGLE PROCESS"}
          </Badge>
        }
        dense
      >
        {loading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> reading the process table…
          </div>
        ) : (
          <Table columns={["RING", "PID", "HEARTBEAT", "STATE", "LATENCY p99", "RESTARTS"]}>
            {ringRows.map((row) => (
              <Row key={row.index}>
                <Cell>
                  <div className="flex min-w-0 items-center gap-2">
                    <StatusDot tone={row.tone} pulse={row.tone === "bull"} />
                    <span className="truncate text-ink-2">{row.name}</span>
                    <span className="truncate font-mono text-[10px] text-ink-3">{row.worker}</span>
                  </div>
                </Cell>
                <Cell align="right" mono tone="muted">
                  {row.pid}
                </Cell>
                <Cell align="right" mono>
                  {row.age}
                </Cell>
                <Cell align="right">
                  <Badge tone={row.tone === "bull" ? "bull" : row.tone === "bear" ? "bear" : "muted"} mono>
                    {row.state}
                  </Badge>
                </Cell>
                <Cell align="right" mono tone={row.index === 1 && row.p99 !== "—" ? "bull" : "neutral"}>
                  {row.p99}
                </Cell>
                <Cell align="right" mono tone="muted">
                  {row.restarts}
                </Cell>
              </Row>
            ))}
          </Table>
        )}
        <p className="px-3 py-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          RING 1 SPINAL p99 &lt;0.05 ms TARGET · NO LLM IN THE MICROSECOND LOOP ·{" "}
          {text(supervisor.law, "sec_004")}
        </p>
      </Card>

      <Card
        title="Gateway latency"
        subtitle="per-ring p99 and the request round trip · core.telemetry"
        icon="activity"
        actions={
          <Badge tone="neutral" mono>
            p50 · p95 · p99
          </Badge>
        }
      >
        <GatewayHealthStats telemetry={telemetry} />
        <div className="mt-3 border-t border-hairline pt-2">
          <RingP99Stats telemetry={telemetry} />
        </div>
      </Card>

      <Card
        title="NiosLifecycleWorker"
        subtitle="TOTP renewal · Binance listenKey · unknown-order retry ladder"
        icon="clock"
        actions={
          <>
            <Badge tone={lifecycle.enabled === true ? "bull" : "muted"} mono>
              {lifecycle.enabled === true ? "ENABLED" : "IDLE"}
            </Badge>
            <Button variant="subtle" size="sm" icon="zap" disabled={busy} onClick={() => void forceTick()}>
              Force lifecycle tick
            </Button>
          </>
        }
      >
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="flex flex-col gap-1 rounded-[4px] border border-hairline bg-canvas px-2.5 py-2">
            <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
              TOTP RENEWAL · 03:00 UTC
            </span>
            <span className="text-[19px] font-semibold tabular-nums text-ink">{duration(totpSeconds)}</span>
            <span className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
              last run {stampNs(asRecord(lifecycle.last_run_utc_ns).totp_renewal)} · runs{" "}
              {int(asRecord(lifecycle.runs).totp_renewal)}
            </span>
          </div>
          <div className="flex flex-col gap-1 rounded-[4px] border border-hairline bg-canvas px-2.5 py-2">
            <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
              BINANCE listenKey · 30 MIN
            </span>
            <span
              className={`text-[19px] font-semibold tabular-nums ${text(listenKey.status, "") === "DEAD" ? "text-bear" : "text-ink"}`}
            >
              {Number.isFinite(listenKeyRemaining) ? duration(listenKeyRemaining) : "—"}
            </span>
            <span className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
              {text(listenKey.status, "—")} · {int(listenKey.pings)} pings
            </span>
          </div>
          <div className="flex flex-col gap-1.5 rounded-[4px] border border-hairline bg-canvas px-2.5 py-2">
            <div className="flex items-center justify-between">
              <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
                UNKNOWN-ORDER RETRY LADDER
              </span>
              <Badge tone={escalated > 0 ? "bear" : openUnknown > 0 ? "accent" : "muted"} mono>
                {escalated > 0 ? "ESCALATED" : openUnknown > 0 ? `${int(openUnknown)} OPEN` : "IDLE"}
              </Badge>
            </div>
            <div className="flex items-center gap-1">
              {ladder.map((step, index) => (
                <span
                  key={step}
                  className={`flex-1 rounded-[2px] border px-1 py-1 text-center text-[10px] tabular-nums ${
                    index === activeRung
                      ? "border-accent bg-elevated text-accent"
                      : "border-hairline bg-surface text-ink-3"
                  }`}
                >
                  {ladderLabel(step)}
                </span>
              ))}
            </div>
            <span className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
              query before retry · {text(unknownOrders.escalation_state, "MANUAL_INTERVENTION_REQUIRED")}
            </span>
          </div>
        </div>
        <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          RUNG = NEXT SCHEDULED PROBE · ATTEMPTS {int(unknownOrders.attempts)} · RESOLVED{" "}
          {int(unknownOrders.resolved)} · ESCALATED {int(unknownOrders.escalated)}
        </p>
      </Card>

      <Card
        title="MiroFish shock simulator"
        subtitle="five archetypes · three ticks · Ring 1 lease revocation"
        icon="flame"
        actions={
          <Badge tone={Number.isFinite(shockLatency) ? (withinSla ? "bull" : "bear") : "muted"} mono>
            {Number.isFinite(shockLatency) ? `${num(shockLatency, 3)} ms` : "—"} · SLA &lt;1.8 ms
          </Badge>
        }
      >
        <div className="grid gap-3 lg:grid-cols-[1fr_1.1fr]">
          <div className="flex flex-col gap-2">
            <div className="grid grid-cols-3 gap-2">
              <Field label="Symbol">
                <TextInput
                  value={shockSymbol}
                  mono
                  onChange={(event) => setShockSymbol(event.target.value.toUpperCase())}
                />
              </Field>
              <Field label="Shock bps" hint="-5000…5000">
                <TextInput value={shockBps} mono onChange={(event) => setShockBps(event.target.value)} />
              </Field>
              <Field label="Silo">
                <SelectInput value={shockSilo} onChange={setShockSilo} options={SILO_OPTIONS} />
              </Field>
            </div>
            <Toolbar>
              <Button
                variant="primary"
                size="sm"
                icon="flame"
                disabled={busy}
                onClick={() => void runShock()}
              >
                Run 3-tick cascade
              </Button>
              <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                5 archetypes · deterministic digest
              </span>
            </Toolbar>
            <div className="rounded-[4px] border border-hairline bg-canvas px-2.5 py-2">
              <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.1em] text-ink-3">
                <span>CASCADE FRAGILITY INDEX</span>
                <span
                  className={`font-semibold tabular-nums ${fragility >= 0.75 ? "text-bear" : "text-ink"}`}
                >
                  {Number.isFinite(fragility) ? num(fragility, 3) : "—"}
                </span>
              </div>
              <div className="mt-1.5">
                <ProgressBar
                  value={Number.isFinite(fragility) ? fragility : 0}
                  max={1}
                  tone={fragility >= 0.75 ? "bear" : "accent"}
                  height={6}
                />
              </div>
              <div className="mt-1 flex items-center justify-between text-[10px] text-ink-3">
                <span>REVOKED LONG LEASES {int(shock?.revoked_long_leases)}</span>
                <span>{shock?.requires_ring1_halt === true ? "RING 1 HALT REQUIRED" : "NO HALT"}</span>
              </div>
            </div>
            {shock !== null ? (
              <span className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
                DIGEST {shortHash(shock.sha256_digest, 12)} · {text(shock.symbol, "—")} ·{" "}
                {text(shock.sentiment_tag, "—")}
              </span>
            ) : null}
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
              PER-ARCHETYPE IMPACT · NET TICK FLOW
            </span>
            {archetypeImpacts.length === 0 ? (
              <EmptyState
                title="No cascade yet"
                hint="Run the shock to allocate the tick flow across the five capital-weighted archetypes."
              />
            ) : (
              archetypeImpacts.map((row) => (
                <div key={row.name} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between text-[11px]">
                    <span className="truncate text-ink-2">{row.name}</span>
                    <span className={`tabular-nums ${row.flow < 0 ? "text-bear" : "text-bull"}`}>
                      {signed(row.flow, 3)}
                    </span>
                  </div>
                  <ProgressBar
                    value={Math.abs(row.flow)}
                    max={row.peak}
                    tone={row.flow < 0 ? "bear" : "bull"}
                    height={3}
                  />
                </div>
              ))
            )}
          </div>
        </div>
      </Card>

      <Card
        title="System 2 investment committee"
        subtitle="ANALYSTS → RESEARCHERS → RISK DEBATE → TRADER · every number re-computed"
        icon="brain"
        actions={
          <Badge
            tone={debateVerdict === "CONVICTED" ? "bull" : debateVerdict === "ABSTAINED" ? "accent" : "muted"}
            mono
          >
            {debateVerdict}
          </Badge>
        }
      >
        <div className="grid gap-2 md:grid-cols-[140px_1fr_1fr_auto]">
          <Field label="Silo">
            <SelectInput value={debateSilo} onChange={setDebateSilo} options={SILO_OPTIONS} />
          </Field>
          <Field label="Symbol" hint="AUTO = top factor candidate">
            <TextInput
              value={debateSymbol}
              mono
              onChange={(event) => setDebateSymbol(event.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Question">
            <TextInput value={debateQuestion} onChange={(event) => setDebateQuestion(event.target.value)} />
          </Field>
          <div className="flex items-end">
            <Button
              variant="primary"
              size="sm"
              icon="brain"
              disabled={busy}
              onClick={() => void runCommittee()}
            >
              Debate
            </Button>
          </div>
        </div>

        {transcript.length === 0 ? (
          <div className="mt-2">
            <EmptyState
              title="No debate yet"
              hint="Run a committee debate to fill the transcript."
            />
          </div>
        ) : (
          <div className="mt-2 grid gap-2 lg:grid-cols-2">
            {COMMITTEE_STAGES.map((group) => {
              const stages = transcript.filter((row) => group.match(text(row.stage, "")));
              if (stages.length === 0) return null;
              return (
                <div key={group.id} className="flex flex-col gap-1.5">
                  <span className="text-[9.5px] uppercase tracking-[0.12em] text-ink-3">{group.label}</span>
                  {stages.map((row, index) => {
                    const stance = stanceOf(row.actor, row.stage, row.text);
                    return (
                      <div
                        key={`${group.id}-${index}`}
                        className="rounded-[4px] border border-hairline bg-canvas px-2.5 py-2"
                      >
                        <div className="mb-1 flex flex-wrap items-center gap-1.5">
                          <Badge tone={stanceTone(stance)} mono>
                            {stance}
                          </Badge>
                          <span className="truncate text-[11px] font-semibold text-ink-2">
                            {text(row.actor, group.label)}
                          </span>
                          <span className="truncate font-mono text-[9.5px] text-ink-3">
                            {text(row.stage, "—")}
                          </span>
                          {row.verified === true ? (
                            <Badge tone="bull" mono>
                              ✓ grounded
                            </Badge>
                          ) : null}
                        </div>
                        <pre className="nios-scroll max-h-40 whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed text-ink-2">
                          {text(row.text, "—")}
                        </pre>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        )}

        <div className="mt-2 grid gap-2 rounded-[4px] border border-hairline bg-elevated px-2.5 py-2 md:grid-cols-4">
          <div>
            <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">DECISION</div>
            <div className="text-[13px] font-semibold text-ink">{upper(debate?.verdict ?? "—")}</div>
          </div>
          <div>
            <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">SYMBOL · SIDE</div>
            <div className="text-[13px] font-semibold text-ink-2">
              {text(debate?.symbol, "—")} · {upper(report.side ?? proposal.side)}
            </div>
          </div>
          <div>
            <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">CAP · POSITION</div>
            <div className="text-[13px] font-semibold tabular-nums text-ink-2">
              {num(report.cap_pct, 2)}% · {num(report.position_pct, 2)}%
            </div>
          </div>
          <div>
            <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">CLAIMS VERIFIED</div>
            <div className="text-[13px] font-semibold tabular-nums text-ink-2">
              {int(report.claims_checked)} · {report.all_stages_verified === true ? "ALL STAGES" : "PARTIAL"}
            </div>
          </div>
        </div>
        {text(debate?.abstention_reason, "").length > 0 ? (
          <Banner tone="bear">{text(debate?.abstention_reason)}</Banner>
        ) : null}
      </Card>

      <div className="flex flex-wrap items-center gap-2 border-t border-hairline pt-2">
        <Badge tone="muted" mono>
          NO BROKER KEYS
        </Badge>
        <Badge tone="muted" mono>
          NO EXCHANGE SOCKETS
        </Badge>
        <Badge tone="muted" mono>
          SHA-256 GROUNDED
        </Badge>
        <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
          RING 2 AIR-GAP ATTESTATION · SIGNS_ORDERS {text(asRecord(seed.air_gap).signs_orders, "false")}
        </span>
      </div>
    </div>
  );
}

export default BrainPage;
