import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "../components/Icon";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  Checkbox,
  EmptyState,
  Field,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextArea,
  TextInput,
  cx,
} from "../components/ui";
import { api, assertFinite, clampSagas, errorMessage } from "../lib/api";
import { asArray, asRecord, bps, isRecord, num, safeNumber, signed, text, timeUtc, units, unitsSigned } from "../lib/format";
import { rowsOf, statusTone } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const HALF_KELLY = 0.5;
const SIM_PATHS = 1200;
const QUOTA_SILOS = [
  { id: "INDIA_ZERODHA_DHAN", short: "INDIA" },
  { id: "US_IBKR", short: "US" },
  { id: "FX_MT5", short: "FX" },
] as const;

interface MissionView {
  id: string;
  silo: string;
  status: string;
  capitalMicros: number;
  targetBps: number;
  progress: number;
  planExit: string;
  symbols: string;
}

interface Feasibility {
  edgeBps: number;
  hitRate: number;
  p5: number;
  p50: number;
  p95: number;
}

function missionView(row: Record<string, unknown>): MissionView {
  const capitalMicros = safeNumber(
    row.capital_units ?? row.capital_micros ?? row.capital,
    0,
  );
  const targetBps = safeNumber(row.target_return_bps ?? row.target_bps, 0);
  const symbolList = asArray(row.symbols)
    .map((entry) => text(entry, ""))
    .filter(Boolean);
  return {
    id: text(row.id ?? row.mission_id, "—"),
    silo: text(row.silo ?? row.silo_id, "—"),
    status: text(row.status ?? row.state, "UNKNOWN").toUpperCase(),
    capitalMicros,
    targetBps,
    progress: safeNumber(row.progress_pct ?? row.completion_pct ?? row.progress, 0),
    planExit: text(row.plan_exit ?? row.plan_exit_note ?? row.exit_plan, "no plan-exit annotation"),
    symbols: symbolList.length > 0 ? symbolList.join(" · ") : text(row.symbols, "—"),
  };
}

function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Deterministic, client-side half-Kelly feasibility fan. Illustrative only. */
function halfKellySim(capitalMicros: number, targetBps: number, tranches: number): Feasibility {
  const slices = Math.max(1, Math.trunc(tranches));
  const target = Math.max(1, targetBps);
  const rng = mulberry32((0x4e494f53 + Math.imul(slices, 2654435761)) >>> 0);
  const perTrancheBps = target / slices;
  const winProb = 0.58;
  const winBps = perTrancheBps * 1.35;
  const lossBps = perTrancheBps * 0.85;
  const sliceMicros = (capitalMicros * HALF_KELLY) / slices;
  const outcomes: number[] = [];
  let edgeSum = 0;
  let wins = 0;
  for (let path = 0; path < SIM_PATHS; path += 1) {
    let bpsTotal = 0;
    for (let slice = 0; slice < slices; slice += 1) {
      const draw = rng();
      if (draw < winProb) {
        bpsTotal += winBps;
        wins += 1;
      } else {
        bpsTotal -= lossBps;
      }
    }
    edgeSum += bpsTotal;
    outcomes.push((sliceMicros * bpsTotal) / 10000);
  }
  outcomes.sort((a, b) => a - b);
  const at = (q: number) => outcomes[Math.min(outcomes.length - 1, Math.max(0, Math.round(q * (outcomes.length - 1))))];
  return {
    edgeBps: edgeSum / SIM_PATHS,
    hitRate: wins / (SIM_PATHS * slices),
    p5: at(0.05),
    p50: at(0.5),
    p95: at(0.95),
  };
}

function quotaOf(source: unknown, siloId: string): { quota: number; used: number } | null {
  const probes = [
    `virtual_margin_quota.${siloId}`,
    `margin_quota.${siloId}`,
    `virtual_margin.${siloId}`,
    `silos.${siloId}`,
  ];
  for (const probe of probes) {
    const record = asRecord(descend(source, probe));
    const quota = safeNumber(record.quota_micros ?? record.quota_units ?? record.quota, Number.NaN);
    if (Number.isFinite(quota)) {
      return { quota, used: safeNumber(record.used_micros ?? record.used_units ?? record.used, 0) };
    }
  }
  return null;
}

function descend(source: unknown, path: string): unknown {
  let cursor: unknown = source;
  for (const part of path.split(".")) {
    if (!isRecord(cursor)) return undefined;
    cursor = cursor[part];
  }
  return cursor;
}

function sagaTone(status: string): "bull" | "warn" | "bear" | "neutral" {
  if (status.includes("COMPLETED")) return "bull";
  if (status.includes("PENDING")) return "warn";
  if (status.includes("COMPENSATION")) return "bear";
  return "neutral";
}

function severityTone(severity: string): "bull" | "warn" | "bear" | "muted" {
  if (severity.includes("CRITICAL") || severity.includes("HIGH") || severity.includes("SEV1")) return "bear";
  if (severity.includes("MEDIUM") || severity.includes("WARN")) return "warn";
  return "muted";
}

export default function MissionsPage({ mode }: PageProps) {
  const now = useTicker(1000);
  const [missionsPayload, setMissionsPayload] = useState<unknown>({});
  const [tranches, setTranches] = useState<Record<string, unknown>[]>([]);
  const [sagas, setSagas] = useState<Record<string, unknown>[]>([]);
  const [postMortems, setPostMortems] = useState<Record<string, unknown>[]>([]);
  const [cockpit, setCockpit] = useState<unknown>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [busy, setBusy] = useState(false);
  const [sagaLimit, setSagaLimit] = useState(100);

  const [silo, setSilo] = useState<string>("INDIA_ZERODHA_DHAN");
  const [capitalUnits, setCapitalUnits] = useState("5000000000");
  const [targetBps, setTargetBps] = useState("850");
  const [maxTranches, setMaxTranches] = useState("3");
  const [symbols, setSymbols] = useState("NIFTY, RELIANCE, HDFCBANK");
  const [notes, setNotes] = useState("");

  const [approving, setApproving] = useState<string | null>(null);
  const [approveCaps, setApproveCaps] = useState("");
  const [approveExcluded, setApproveExcluded] = useState("");
  const [approveReserve, setApproveReserve] = useState("1500");
  const [approveConfirmed, setApproveConfirmed] = useState(false);

  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeReason, setRevokeReason] = useState("");

  const [closeSagaId, setCloseSagaId] = useState("");
  const [closeSymbol, setCloseSymbol] = useState("");
  const [closeReason, setCloseReason] = useState("");

  const load = useCallback(async () => {
    try {
      const settled = await Promise.allSettled([
        api.get<unknown>("/missions"),
        api.get<unknown>("/tranches"),
        api.get<unknown>("/trades/sagas", { limit: clampSagas(sagaLimit) }),
        api.get<unknown>("/post-mortems", { limit: 20 }),
        api.get<unknown>("/cockpit/state"),
      ]);
      const [missionsResult, tranchesResult, sagasResult, pmResult, cockpitResult] = settled;
      if (missionsResult.status === "fulfilled") setMissionsPayload(missionsResult.value);
      if (tranchesResult.status === "fulfilled") setTranches(rowsOf(tranchesResult.value));
      if (sagasResult.status === "fulfilled") setSagas(rowsOf(sagasResult.value));
      if (pmResult.status === "fulfilled") setPostMortems(rowsOf(pmResult.value));
      if (cockpitResult.status === "fulfilled") setCockpit(cockpitResult.value);
      const failures = settled.filter((entry) => entry.status === "rejected");
      setError(
        failures.length === settled.length
          ? errorMessage((failures[0] as PromiseRejectedResult).reason)
          : null,
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [sagaLimit]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
    }, 12000);
    return () => clearInterval(timer);
  }, [load]);

  const missions = useMemo(() => rowsOf(missionsPayload).map(missionView), [missionsPayload]);
  const deployed = missions.reduce((total, mission) => total + mission.capitalMicros, 0);
  const activeMissions = missions.filter((mission) => !/(CLOSED|REVOKED|EXPIRED)/.test(mission.status)).length;
  const pendingSagas = sagas.filter((row) => text(row.status).toUpperCase().includes("PENDING")).length;

  const parsedCapital = safeNumber(capitalUnits, 0);
  const parsedTarget = safeNumber(targetBps, 0);
  const parsedTranches = Math.min(12, Math.max(1, Math.trunc(safeNumber(maxTranches, 1))));
  const feasibility = useMemo(
    () => halfKellySim(parsedCapital, parsedTarget, parsedTranches),
    [parsedCapital, parsedTarget, parsedTranches],
  );

  const quotas = useMemo(
    () =>
      QUOTA_SILOS.map((entry) => {
        const fromCockpit = quotaOf(cockpit, entry.id);
        const fromMissions = quotaOf(missionsPayload, entry.id);
        return { ...entry, quota: fromCockpit ?? fromMissions };
      }),
    [cockpit, missionsPayload],
  );

  async function createMission() {
    if (busy) return;
    setBusy(true);
    try {
      const capital = assertFinite(capitalUnits, "capital_units");
      if (capital <= 0) throw new Error("capital_units must be greater than zero");
      const target = assertFinite(targetBps, "target_return_bps");
      if (target <= 0) throw new Error("target_return_bps must be greater than zero");
      const trancheCount = assertFinite(maxTranches, "max_tranches");
      if (trancheCount < 1) throw new Error("max_tranches must be at least 1");
      const symbolList = symbols.split(",").map((entry) => entry.trim().toUpperCase()).filter(Boolean);
      if (symbolList.length === 0) throw new Error("at least one symbol is required");
      await api.post("/missions", {
        silo,
        capital_units: capital,
        target_return_bps: target,
        max_tranches: Math.trunc(trancheCount),
        half_kelly: true,
        symbols: symbolList,
        notes,
      });
      setNote({ tone: "bull", message: `Mission drafted for ${silo} · ${symbolList.join(", ")}` });
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  function beginApprove(row: Record<string, unknown>, view: MissionView) {
    setRevoking(null);
    setApproving(view.id);
    setApproveConfirmed(false);
    setApproveCaps(
      asArray(row.tranche_caps_micros)
        .map((entry) => text(entry, ""))
        .filter(Boolean)
        .join(", ") || text(row.tranche_caps_micros ?? row.capital_units, ""),
    );
    const excludedList = asArray(row.excluded_symbols)
      .map((entry) => text(entry, ""))
      .filter(Boolean);
    setApproveExcluded(excludedList.length > 0 ? excludedList.join(", ") : text(row.excluded_symbols, ""));
    setApproveReserve(text(row.reserve_split_bps ?? row.reserve_split_override_bps, "1500"));
  }

  async function approveMission(id: string) {
    if (busy) return;
    setBusy(true);
    try {
      const caps = approveCaps
        .split(",")
        .map((entry) => entry.trim())
        .filter(Boolean)
        .map((entry) => assertFinite(entry, "tranche_caps_micros"));
      if (caps.length === 0) throw new Error("at least one tranche cap is required");
      const excluded = approveExcluded
        .split(",")
        .map((entry) => entry.trim().toUpperCase())
        .filter(Boolean);
      const reserve = assertFinite(approveReserve, "reserve_split_override_bps");
      await api.post(`/missions/${encodeURIComponent(id)}/approve`, {
        tranche_caps_micros: caps,
        excluded_symbols: excluded,
        reserve_split_override_bps: reserve,
      });
      setNote({ tone: "bull", message: `Mission ${id} approved with ${caps.length} tranche caps` });
      setApproving(null);
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function revokeMission(id: string) {
    if (busy) return;
    setBusy(true);
    try {
      if (revokeReason.trim().length < 3) throw new Error("a revoke reason of at least 3 characters is required");
      await api.post(`/missions/${encodeURIComponent(id)}/revoke`, { reason: revokeReason.trim() });
      setNote({ tone: "bull", message: `Mission ${id} revoked` });
      setRevoking(null);
      setRevokeReason("");
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function rearm(trancheId: string) {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/tranches/${encodeURIComponent(trancheId)}/rearm`, {});
      setNote({ tone: "bull", message: `Tranche ${trancheId} re-armed` });
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  async function closeTrade() {
    if (busy) return;
    setBusy(true);
    try {
      if (!closeSagaId) throw new Error("select a saga to close");
      const symbol = closeSymbol.trim().toUpperCase();
      if (!symbol) throw new Error("symbol is required");
      if (closeReason.trim().length < 3) throw new Error("close reason of at least 3 characters is required");
      await api.post("/control/trades/close", {
        saga_id: closeSagaId,
        symbol,
        idempotency_key: `close-${closeSagaId}-${Date.now()}`,
        reason: closeReason.trim(),
      });
      setNote({ tone: "bull", message: `Close submitted for ${symbol} (saga ${closeSagaId})` });
      setCloseReason("");
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="MISSIONS"
        subtitle="Multi-tranche mission, profit-split saga & autopilot orchestrator"
        badges={
          <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
            {mode}
          </Badge>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3 tabular-nums">
              {new Date(now).toISOString().slice(11, 19)} UTC
            </span>
            <Button icon="refresh" variant="subtle" size="sm" onClick={() => void load()}>
              Refresh
            </Button>
          </div>
        }
      />
      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat label="Active Missions" value={activeMissions} hint={`${missions.length} in book`} tone="accent" />
        <Stat label="Capital Committed" value={units(deployed)} hint="micro-units across missions" />
        <Stat label="Pending Sweeps" value={pendingSagas} hint="profit-split sagas awaiting verification" tone={pendingSagas > 0 ? "bear" : "bull"} />
        <Stat
          label="Half-Kelly Edge"
          value={`${signed(feasibility.edgeBps, 1)} bps`}
          hint={`hit ${num(feasibility.hitRate * 100, 1)}% · kelly 0.50`}
          tone={feasibility.edgeBps >= 0 ? "bull" : "bear"}
        />
      </StatGrid>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Card
          title="Mission Book"
          subtitle="approve · cap tranches · revoke"
          actions={<Badge tone="muted" mono>{`${missions.length} MISSIONS`}</Badge>}
        >
          {loading ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading missions
            </div>
          ) : missions.length === 0 ? (
            <EmptyState title="No missions" hint="Draft a mission to open a tranche ladder." />
          ) : (
            <div className="grid min-w-0 grid-cols-1 gap-2 md:grid-cols-2">
              {missions.map((mission) => {
                return (
                  <div key={mission.id} className="min-w-0 rounded-[4px] border border-hairline bg-canvas p-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-[11px] text-ink">{mission.id}</span>
                      <div className="flex shrink-0 items-center gap-1">
                        <Badge tone={statusTone(mission.status)}>{mission.status}</Badge>
                        <Badge tone="muted">{mission.silo}</Badge>
                      </div>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
                      <span>Capital</span>
                      <span className="text-right text-[11px] normal-case tracking-normal text-ink tabular-nums">{units(mission.capitalMicros)}</span>
                      <span>Target</span>
                      <span className="text-right text-[11px] normal-case tracking-normal text-accent tabular-nums">{bps(mission.targetBps)}</span>
                      <span>Symbols</span>
                      <span className="truncate text-right text-[11px] normal-case tracking-normal text-ink-2">{mission.symbols}</span>
                    </div>
                    <div className="mt-2">
                      <ProgressBar
                        value={mission.progress}
                        max={100}
                        tone={mission.progress >= 100 ? "bull" : "accent"}
                        label={`PROGRESS ${num(mission.progress, 1)}%`}
                      />
                    </div>
                    <div className="mt-1.5 flex items-start gap-1 text-[10px] leading-snug text-ink-3">
                      <Icon name="target" size={11} className="mt-[1px] shrink-0" />
                      <span className="truncate">{mission.planExit}</span>
                    </div>
                    <div className="mt-2 flex items-center gap-1.5">
                      <Button size="sm" variant="subtle" icon="check" onClick={() => beginApprove(findMissionRow(missionsPayload, mission.id), mission)}>
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        icon="close"
                        onClick={() => {
                          setApproving(null);
                          setRevoking(mission.id);
                          setRevokeReason("");
                        }}
                      >
                        Revoke
                      </Button>
                    </div>
                    {approving === mission.id ? (
                      <div className="mt-2 border-t border-hairline pt-2">
                        <div className="grid grid-cols-1 gap-1.5">
                          <Field label="Tranche Caps (micros, csv)">
                            <TextInput mono value={approveCaps} onChange={(event) => setApproveCaps(event.target.value)} />
                          </Field>
                          <Field label="Excluded Symbols (csv)">
                            <TextInput mono value={approveExcluded} onChange={(event) => setApproveExcluded(event.target.value)} />
                          </Field>
                          <Field label="Reserve Split Override (bps)">
                            <TextInput mono value={approveReserve} onChange={(event) => setApproveReserve(event.target.value)} />
                          </Field>
                          <Checkbox
                            checked={approveConfirmed}
                            onChange={setApproveConfirmed}
                            label="I verified caps, exclusions and the reserve override for this mission."
                          />
                          <div className="flex items-center gap-1.5">
                            <Button
                              size="sm"
                              variant="primary"
                              disabled={!approveConfirmed || busy}
                              onClick={() => void approveMission(mission.id)}
                            >
                              Confirm Approve
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => setApproving(null)}>
                              Cancel
                            </Button>
                          </div>
                        </div>
                      </div>
                    ) : null}
                    {revoking === mission.id ? (
                      <div className="mt-2 border-t border-hairline pt-2">
                        <Field label="Revoke Reason">
                          <TextInput value={revokeReason} onChange={(event) => setRevokeReason(event.target.value)} />
                        </Field>
                        <div className="mt-1.5 flex items-center gap-1.5">
                          <Button size="sm" variant="danger" disabled={busy} onClick={() => void revokeMission(mission.id)}>
                            Confirm Revoke
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setRevoking(null)}>
                            Cancel
                          </Button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card title="New Mission" subtitle="draft → approve is a two-step ceremony">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Silo" className="col-span-2">
              <SelectInput
                value={silo}
                onChange={setSilo}
                options={QUOTA_SILOS.map((entry) => ({ value: entry.id, label: entry.short }))}
              />
            </Field>
            <Field label="Capital (micros)">
              <TextInput mono value={capitalUnits} onChange={(event) => setCapitalUnits(event.target.value)} />
            </Field>
            <Field label="Target (bps)">
              <TextInput mono value={targetBps} onChange={(event) => setTargetBps(event.target.value)} />
            </Field>
            <Field label="Max Tranches">
              <TextInput mono value={maxTranches} onChange={(event) => setMaxTranches(event.target.value)} />
            </Field>
            <Field label="Half Kelly">
              <div className="flex h-7 items-center text-[11px] text-bull">LOCKED ON</div>
            </Field>
            <Field label="Symbols (csv)" className="col-span-2">
              <TextInput mono value={symbols} onChange={(event) => setSymbols(event.target.value)} />
            </Field>
            <Field label="Notes" className="col-span-2">
              <TextArea value={notes} rows={2} onChange={setNotes} placeholder="plan-exit, tranche ladder, reserve intent" />
            </Field>
          </div>
          <div className="mt-2 rounded-[4px] border border-hairline bg-canvas p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">Half-Kelly Monte Carlo</span>
              <Badge tone="muted">ILLUSTRATIVE — NOT A FORECAST</Badge>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
              <span>Expected Edge</span>
              <span className={cx("text-right text-[11px] normal-case tracking-normal tabular-nums", feasibility.edgeBps >= 0 ? "text-bull" : "text-bear")}>
                {signed(feasibility.edgeBps, 1)} bps
              </span>
              <span>Kelly Fraction</span>
              <span className="text-right text-[11px] normal-case tracking-normal text-ink tabular-nums">0.50</span>
              <span>P5 / P50 / P95</span>
              <span className="text-right text-[10px] normal-case tracking-normal text-ink-2 tabular-nums">
                {unitsSigned(feasibility.p5, 1)} · {unitsSigned(feasibility.p50, 1)} · {unitsSigned(feasibility.p95, 1)}
              </span>
            </div>
          </div>
          <Button className="mt-2 w-full" variant="primary" disabled={busy} icon="plus" onClick={() => void createMission()}>
            Draft Mission
          </Button>
        </Card>
      </div>

      <Card title="Virtual Margin Quota" subtitle="headroom before a regulated silo needs capital" icon="shield">
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {quotas.map((entry) => {
            const headroom = entry.quota ? entry.quota.quota - entry.quota.used : null;
            return (
              <div key={entry.id} className="rounded-[4px] border border-hairline bg-canvas p-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-ink-2">{entry.short}</span>
                  <Badge tone={headroom !== null && headroom > 0 ? "bull" : headroom === null ? "muted" : "bear"}>
                    {headroom === null ? "NO DATA" : "QUOTA"}
                  </Badge>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
                  <span>Quota</span>
                  <span>Used</span>
                  <span className="text-right">Headroom</span>
                </div>
                <div className="mt-0.5 grid grid-cols-3 gap-1 text-[11px] tabular-nums">
                  <span className="text-ink">{entry.quota ? units(entry.quota.quota) : "—"}</span>
                  <span className="text-ink-2">{entry.quota ? units(entry.quota.used) : "—"}</span>
                  <span className={cx("text-right", headroom === null ? "text-ink-3" : headroom > 0 ? "text-bull" : "text-bear")}>
                    {headroom === null ? "—" : units(headroom)}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Tranche Risk Isolation" subtitle="heat cap · utilisation · re-arm" dense>
        {loading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> loading tranches
          </div>
        ) : tranches.length === 0 ? (
          <EmptyState title="No tranches" hint="Approve a mission to spawn tranches." />
        ) : (
          <Table
            columns={["Tranche", "Mission", "Silo", "Heat bps", "Cap", "Utilisation", "State", ""]}
          >
            {tranches.map((row, index) => {
              const id = text(row.tranche_id ?? row.id, `T-${index + 1}`);
              const heat = safeNumber(row.heat_bps ?? row.heat, 0);
              const cap = safeNumber(row.cap_micros ?? row.cap_units ?? row.cap, 0);
              const utilisation = safeNumber(row.utilisation_pct ?? row.utilization_pct ?? row.utilisation, 0);
              const state = text(row.state ?? row.status, "UNKNOWN").toUpperCase();
              return (
                <Row key={`${id}-${index}`}>
                  <Cell mono tone="accent">{id}</Cell>
                  <Cell mono>{text(row.mission_id ?? row.mission, "—")}</Cell>
                  <Cell tone="muted">{text(row.silo ?? row.silo_id, "—")}</Cell>
                  <Cell align="right" tone={heat > 600 ? "bear" : "neutral"}>{num(heat, 1)}</Cell>
                  <Cell align="right">{units(cap)}</Cell>
                  <Cell className="w-[160px]">
                    <ProgressBar value={utilisation} max={100} height={3} tone={utilisation > 85 ? "bear" : "accent"} label={`${num(utilisation, 1)}%`} />
                  </Cell>
                  <Cell>
                    <Badge tone={statusTone(state)}>{state}</Badge>
                  </Cell>
                  <Cell align="right">
                    <Button size="sm" variant="subtle" icon="refresh" disabled={busy} onClick={() => void rearm(id)}>
                      Rearm
                    </Button>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Card
          title="Profit-Split Saga Ledger"
          subtitle="close fill → dynamic split → custody sweep"
          actions={
            <SelectInput
              className="w-[110px]"
              value={String(sagaLimit)}
              onChange={(value) => setSagaLimit(Number(value))}
              options={[25, 100, 250, 1000].map((value) => ({ value: String(value), label: `LIMIT ${value}` }))}
            />
          }
        >
          <div className="mb-2 grid grid-cols-1 gap-1.5 md:grid-cols-[minmax(0,1fr)_110px_minmax(0,1fr)_auto]">
            <SelectInput
              value={closeSagaId}
              onChange={(value) => {
                setCloseSagaId(value);
                const match = sagas.find((row) => text(row.saga_id ?? row.id) === value);
                setCloseSymbol(text(match?.symbol, ""));
              }}
              options={[{ value: "", label: "SELECT SAGA" }].concat(
                sagas.slice(0, 50).map((row) => {
                  const id = text(row.saga_id ?? row.id, "—");
                  return { value: id, label: id };
                }),
              )}
            />
            <TextInput mono placeholder="SYMBOL" value={closeSymbol} onChange={(event) => setCloseSymbol(event.target.value)} />
            <TextInput placeholder="close reason" value={closeReason} onChange={(event) => setCloseReason(event.target.value)} />
            <Button size="sm" variant="danger" disabled={busy} onClick={() => void closeTrade()}>
              Close Trade
            </Button>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading sagas
            </div>
          ) : sagas.length === 0 ? (
            <EmptyState title="No sagas" hint="Closed trades emit a profit-split saga here." />
          ) : (
            <Table columns={["Saga", "Status", "Custody Sweep", "Gross", "Net", "Symbol"]}>
              {sagas.slice(0, 60).map((row, index) => {
                const id = text(row.saga_id ?? row.id, `SAGA-${index + 1}`);
                const status = text(row.status ?? row.state, "UNKNOWN").toUpperCase();
                return (
                  <Row key={`${id}-${index}`}>
                    <Cell mono>{id}</Cell>
                    <Cell>
                      <Badge tone={sagaTone(status)}>{status}</Badge>
                    </Cell>
                    <Cell mono tone="muted">
                      {text(row.sweep_direction ?? row.custody_direction, "UMFUTURE_MAIN -> SPOT")}
                    </Cell>
                    <Cell align="right">{units(safeNumber(row.gross_micros ?? row.gross_units ?? row.gross, 0))}</Cell>
                    <Cell align="right" tone={safeNumber(row.net_micros ?? row.net_units ?? row.net, 0) >= 0 ? "bull" : "bear"}>
                      {unitsSigned(safeNumber(row.net_micros ?? row.net_units ?? row.net, 0))}
                    </Cell>
                    <Cell mono tone="muted">{text(row.symbol, "—")}</Cell>
                  </Row>
                );
              })}
            </Table>
          )}
        </Card>

        <Card title="Post-Mortem Feed" subtitle="losses, slippage spikes, rejections" icon="logbook">
          {postMortems.length === 0 ? (
            <EmptyState title="No post-mortems" hint="The learning hook writes here after a loss." />
          ) : (
            <div className="flex flex-col gap-1.5">
              {postMortems.slice(0, 10).map((row, index) => {
                const severity = text(row.severity ?? row.level, "INFO").toUpperCase();
                return (
                  <div key={`${text(row.id, "pm")}-${index}`} className="rounded-[4px] border border-hairline bg-canvas p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-[10.5px] text-ink-2">
                        {text(row.symbol ?? row.entity ?? row.asset, "PORTFOLIO")}
                      </span>
                      <Badge tone={severityTone(severity)}>{severity}</Badge>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[10.5px] leading-snug text-ink-3">
                      {text(row.summary ?? row.reason ?? row.detail ?? row.message, "no summary")}
                    </p>
                    <div className="mt-1 flex items-center justify-between text-[9.5px] uppercase tracking-[0.08em] text-ink-3">
                      <span>{text(row.kind ?? row.event_type, "EVENT")}</span>
                      <span className="tabular-nums">{timeUtc(row.created_at ?? row.occurred_at ?? row.ts)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function findMissionRow(payload: unknown, id: string): Record<string, unknown> {
  return rowsOf(payload).find((row) => text(row.id ?? row.mission_id) === id) ?? {};
}
