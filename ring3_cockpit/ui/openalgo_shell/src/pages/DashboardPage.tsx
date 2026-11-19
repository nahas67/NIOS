import { useCallback, useEffect, useMemo, useState } from "react";

import { EquityDrawdownCurve } from "../charts";
import { SILOS, VAULTS } from "../config/nav";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  KeyValue,
  PageHeader,
  ProgressBar,
  Row,
  Spinner,
  Stat,
  StatGrid,
  StatusDot,
  Table,
} from "../components/ui";
import { api, errorMessage } from "../lib/api";
import {
  asRecord,
  asRows,
  bps,
  clockNs,
  int,
  num,
  pct,
  safeNumber,
  text,
  usd,
  usdSigned,
} from "../lib/format";
import { drawdownBps, navMicros, pnlMicros, preflightOf, rowsOf } from "../lib/payload";
import { fetchCockpitOverview, mergeTelemetry, telemetryOf, useGatewayTelemetry } from "../lib/telemetry";
import { SSE_TYPES, useMarketStream } from "../lib/stream";
import { GatewayHealthStats, RingP99Stats } from "../components/telemetry";
import { useTicker } from "../state/session";
import type { PageProps } from "./types";

interface Note {
  tone: "neutral" | "bull" | "bear";
  message: string;
}

const RING_BOXES = [
  { ring: "RING 0", label: "SOVEREIGN VAULT", detail: "signs · settles · keys", x: 246, y: 16 },
  { ring: "RING 1", label: "SPINAL REFLEX", detail: "<0.05ms hot path", x: 470, y: 102 },
  { ring: "RING 3", label: "GLASS COCKPIT", detail: "operator control", x: 246, y: 188 },
  { ring: "RING 2", label: "BICAMERAL BRAIN", detail: "grounded reasoning", x: 12, y: 102 },
] as const;

const RING_LINKS = [
  { from: [456, 44], to: [518, 130] },
  { from: [544, 130], to: [456, 216] },
  { from: [246, 216], to: [132, 130] },
  { from: [112, 102], to: [246, 44] },
] as const;

function siloRowsOf(payload: unknown): Record<string, unknown>[] {
  const silos = asRecord(payload).silos;
  if (Array.isArray(silos)) return silos.map((entry) => asRecord(entry));
  const mapped = Object.entries(asRecord(silos)).map(([id, value]) => ({ silo: id, ...asRecord(value) }));
  if (mapped.length > 0) return mapped;
  return rowsOf(payload).filter((row) => typeof row.silo === "string");
}

function vaultMicros(row: Record<string, unknown>, vault: string): number {
  const entry = asRecord(row.vaults)[vault];
  const direct = safeNumber(entry, Number.NaN);
  if (Number.isFinite(direct)) return direct;
  return safeNumber(asRecord(entry).micros, Number.NaN);
}

function siloNav(row: Record<string, unknown>): number {
  return safeNumber(row.nav_micros, safeNumber(asRecord(row.vaults).nav_micros, 0));
}

function framePreview(data: Record<string, unknown>): string {
  try {
    const body = JSON.stringify(data);
    return body.length <= 92 ? body : `${body.slice(0, 89)}…`;
  } catch {
    return "—";
  }
}

function streamTone(type: string): "neutral" | "accent" | "bull" | "bear" | "muted" {
  if (type.includes("ALERT") || type.includes("HALT")) return "bear";
  if (type.includes("PROGRESS") || type.includes("SWEEP")) return "bull";
  if (type.includes("ORDER") || type.includes("SIGNAL") || type.includes("RECON")) return "accent";
  return "muted";
}

export default function DashboardPage({ mode }: PageProps) {
  const tick = useTicker(1000);
  const stream = useMarketStream({ max: 60 });
  const gatewayTelemetry = useGatewayTelemetry();
  const [workspace, setWorkspace] = useState<unknown>({});
  const [cockpit, setCockpit] = useState<unknown>({});
  const [overview, setOverview] = useState<unknown>(null);
  const [performance, setPerformance] = useState<unknown>({});
  const [missions, setMissions] = useState<unknown>({});
  const [preflight, setPreflight] = useState<unknown>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);

  const load = useCallback(async () => {
    const [state, rings, perf, missionRows, gates, aggregate] = await Promise.allSettled([
      api.get<unknown>(`/workspace/${mode === "LIVE" ? "live" : "paper"}/state`),
      api.get<unknown>("/cockpit/state"),
      api.get<unknown>("/analytics/performance", { bucket: "1D" }),
      api.get<unknown>("/missions"),
      api.get<unknown>("/control/preflight"),
      fetchCockpitOverview(),
    ]);
    const failures: string[] = [];
    if (state.status === "fulfilled") setWorkspace(state.value);
    else failures.push(`workspace: ${errorMessage(state.reason)}`);
    if (rings.status === "fulfilled") setCockpit(rings.value);
    else failures.push(`cockpit: ${errorMessage(rings.reason)}`);
    if (perf.status === "fulfilled") setPerformance(perf.value);
    else failures.push(`analytics: ${errorMessage(perf.reason)}`);
    if (missionRows.status === "fulfilled") setMissions(missionRows.value);
    else failures.push(`missions: ${errorMessage(missionRows.reason)}`);
    if (gates.status === "fulfilled") setPreflight(gates.value);
    else failures.push(`preflight: ${errorMessage(gates.reason)}`);
    // The aggregate telemetry surface is optional: a missing route is not an error.
    if (aggregate.status === "fulfilled" && aggregate.value !== null) setOverview(aggregate.value);
    setError(failures.length === 5 ? failures.join(" · ") : null);
    setLoading(false);
  }, [mode]);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 10_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const siloRows = useMemo(() => siloRowsOf(workspace), [workspace]);
  const hasNav = useMemo(() => {
    if (navMicros(workspace) > 0) return true;
    return siloRows.some(
      (row) => row.nav_micros !== undefined || asRecord(row.vaults).nav_micros !== undefined,
    );
  }, [workspace, siloRows]);
  const totalNav = useMemo(() => {
    const direct = navMicros(workspace);
    return direct > 0 ? direct : siloRows.reduce((sum, row) => sum + siloNav(row), 0);
  }, [workspace, siloRows]);
  const freeMargin = useMemo(() => {
    const candidates = [
      asRecord(workspace).free_margin_micros,
      asRecord(asRecord(workspace).risk_telemetry).free_margin_micros,
      asRecord(cockpit).free_margin_micros,
    ];
    for (const candidate of candidates) {
      const value = safeNumber(candidate, Number.NaN);
      if (Number.isFinite(value)) return value;
    }
    return null;
  }, [workspace, cockpit]);

  const equity = useMemo(() => {
    return asRows(asRecord(performance).equity_curve).map((row) => safeNumber(row.nav_micros, 0) / 1_000_000);
  }, [performance]);
  const drawdown = useMemo(() => {
    return asRows(asRecord(performance).underwater).map((row) => Math.abs(safeNumber(row.drawdown_pct, 0)));
  }, [performance]);
  const realised = useMemo(() => {
    const direct = pnlMicros(workspace, true);
    return direct !== 0 ? direct : safeNumber(asRecord(performance).net_pnl_micros, 0);
  }, [workspace, performance]);
  const maxDrawdownBps = useMemo(() => {
    const direct = drawdownBps(workspace);
    return direct !== 0 ? direct : safeNumber(asRecord(performance).max_drawdown_pct, 0) * 100;
  }, [workspace, performance]);

  const supervisor = asRecord(asRecord(cockpit).supervisor);
  const ringRows = asRows(supervisor.rings);
  const preflightSummary = preflightOf(preflight);
  const missionRows = rowsOf(missions);
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const frame of stream.frames) out[frame.type] = (out[frame.type] ?? 0) + 1;
    return out;
  }, [stream.frames]);

  const telemetry = useMemo(
    () => mergeTelemetry(telemetryOf(overview), telemetryOf(cockpit), gatewayTelemetry),
    [overview, cockpit, gatewayTelemetry],
  );

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="DASHBOARD"
        subtitle="4-ring OS isolation, IBOR NAV, silo × vault matrix and the live 12-topic stream"
        badges={
          <div className="flex items-center gap-1.5">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone={stream.status === "open" ? "bull" : stream.status === "error" ? "bear" : "muted"}>
              SSE {stream.status.toUpperCase()}
            </Badge>
          </div>
        }
        actions={
          <Button
            icon="refresh"
            size="sm"
            variant="subtle"
            onClick={() => {
              void load();
              setNote({
                tone: "neutral",
                message: "Workspace, cockpit, analytics and gate snapshot refreshed.",
              });
            }}
          >
            Refresh
          </Button>
        }
      />

      {error !== null ? <Banner tone="bear">{error}</Banner> : null}
      {note !== null ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat
          label="Total IBOR NAV"
          value={hasNav ? usd(totalNav) : "—"}
          hint={`${int(siloRows.length)} silos · ${mode} workspace`}
        />
        <Stat
          label="Free Margin"
          value={freeMargin === null ? "—" : usd(freeMargin)}
          tone={freeMargin === null ? "muted" : "neutral"}
          hint={freeMargin === null ? "not reported by this workspace" : "risk_telemetry.free_margin_micros"}
        />
        <Stat
          label="Realized PnL"
          value={usdSigned(realised)}
          tone={realised > 0 ? "bull" : realised < 0 ? "bear" : "muted"}
          hint="statutory cost already deducted"
        />
        <Stat
          label="Max Drawdown"
          value={bps(maxDrawdownBps, 1)}
          tone={maxDrawdownBps > 0 ? "bear" : "muted"}
          hint="peak-to-trough, basis points"
        />
      </StatGrid>

      <Card
        title="Gateway Telemetry"
        subtitle="measured on the real hot paths · core.telemetry · 10 s poll"
        icon="activity"
        actions={
          <Badge tone="neutral" mono>
            REAL MEASUREMENT
          </Badge>
        }
      >
        <GatewayHealthStats telemetry={telemetry} />
        <div className="mt-3 border-t border-hairline pt-2">
          <RingP99Stats telemetry={telemetry} />
        </div>
      </Card>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <Card
          title="4-Ring OS Isolation"
          subtitle="sec_004 · physically separate processes, SHM kill latch"
          icon="shield"
        >
          <svg viewBox="0 0 640 260" className="w-full" role="img" aria-label="four ring topology">
            {RING_LINKS.map((link) => (
              <line
                key={`${link.from[0]}-${link.from[1]}`}
                x1={link.from[0]}
                y1={link.from[1]}
                x2={link.to[0]}
                y2={link.to[1]}
                stroke="#27292E"
                strokeWidth={1.5}
                fill="none"
              />
            ))}
            {RING_BOXES.map((box) => {
              const active = box.ring === "RING 3";
              return (
                <g key={box.ring}>
                  <rect
                    x={box.x}
                    y={box.y}
                    width={172}
                    height={64}
                    fill="none"
                    stroke={active ? "#F96316" : "#27292E"}
                    strokeWidth={1.5}
                  />
                  <text
                    x={box.x + 10}
                    y={box.y + 20}
                    fill={active ? "#F96316" : "#8C9099"}
                    fontSize="9.5"
                    letterSpacing="1.4"
                  >
                    {box.ring}
                  </text>
                  <text
                    x={box.x + 10}
                    y={box.y + 38}
                    fill="#F4F5F7"
                    fontSize="11"
                    fontWeight="600"
                    letterSpacing="0.6"
                  >
                    {box.label}
                  </text>
                  <text x={box.x + 10} y={box.y + 54} fill="#5A5E66" fontSize="9">
                    {box.detail}
                  </text>
                </g>
              );
            })}
          </svg>
          <div className="mt-2 grid grid-cols-2 gap-2 border-t border-hairline pt-2 xl:grid-cols-4">
            <KeyValue
              columns={1}
              items={[{ label: "Distinct OS procs", value: num(supervisor.distinct_os_processes, 0) }]}
            />
            <KeyValue
              columns={1}
              items={[
                {
                  label: "Four-ring isolation",
                  value: supervisor.four_ring_isolation === true ? "TRUE" : "NOT ATTACHED",
                  tone: supervisor.four_ring_isolation === true ? "bull" : "muted",
                },
              ]}
            />
            <KeyValue columns={1} items={[{ label: "SHM prefix", value: text(supervisor.shm_prefix) }]} />
            <KeyValue columns={1} items={[{ label: "Clock UTC", value: clockNs(tick * 1_000_000) }]} />
          </div>
        </Card>

        <Card
          title="Ring Heartbeat"
          subtitle="shared-memory heartbeat ages per ring process"
          icon="activity"
          actions={
            <Badge tone={ringRows.length > 0 ? "bull" : "muted"} mono>
              {ringRows.length} RINGS
            </Badge>
          }
        >
          {loading && ringRows.length === 0 ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading heartbeat table
            </div>
          ) : ringRows.length === 0 ? (
            <EmptyState
              title="Supervisor not attached"
              hint="Rings run in single-process mode; the heartbeat table appears when the OS supervisor is wired."
            />
          ) : (
            <Table columns={["RING", "WORKER", "PID", "AGE"]} dense>
              {ringRows.map((row, index) => {
                const alive =
                  row.alive === true ||
                  row.healthy === true ||
                  safeNumber(row.heartbeat_age_ms, Number.NaN) < 5_000;
                return (
                  <Row key={`${text(row.worker, "ring")}-${index}`}>
                    <Cell mono>{text(row.ring, `R${index}`)}</Cell>
                    <Cell>
                      <span className="flex items-center gap-1.5">
                        <StatusDot tone={alive ? "bull" : "bear"} pulse={alive} />
                        {text(row.worker, "—")}
                      </span>
                    </Cell>
                    <Cell mono align="right">
                      {text(row.pid, "—")}
                    </Cell>
                    <Cell align="right" tone={alive ? "neutral" : "bear"}>
                      {num(row.heartbeat_age_ms, 1)} ms
                    </Cell>
                  </Row>
                );
              })}
            </Table>
          )}
        </Card>
      </div>

      <Card
        title="Silo × Vault Matrix"
        subtitle="four silos × four vaults · integer micro-unit balances, in-silo compounding only"
        icon="ledger"
        dense
        actions={
          <Badge tone="accent" mono>
            {SILOS.length}×{VAULTS.length}
          </Badge>
        }
      >
        {loading && siloRows.length === 0 ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> reading ledger vaults
          </div>
        ) : siloRows.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="No silo balances"
              hint="The workspace state returned no silo rows; fund the paper ledger to populate the matrix."
            />
          </div>
        ) : (
          <Table columns={["SILO", ...VAULTS, "TAX FLOOR"]}>
            {siloRows.map((row, index) => {
              const siloId = text(row.silo ?? row.silo_id, SILOS[index]?.id ?? "—");
              const floor = safeNumber(
                row.floor_bps,
                SILOS.find((silo) => silo.id === siloId)?.floorBps ?? 0,
              );
              return (
                <Row key={siloId}>
                  <Cell>
                    <div className="flex flex-col">
                      <span className="text-ink">{siloId}</span>
                      <span className="text-[10px] text-ink-3">
                        NAV {usd(siloNav(row))} · {text(row.mode, "PAPER")}
                      </span>
                    </div>
                  </Cell>
                  {VAULTS.map((vault) => {
                    const micros = vaultMicros(row, vault);
                    const margin = safeNumber(
                      asRecord(asRecord(row.vaults)[vault]).margin_micros,
                      Number.NaN,
                    );
                    return (
                      <Cell key={vault} align="right">
                        <div className="flex flex-col">
                          <span className="text-ink">{usd(micros)}</span>
                          <span className="text-[10px] text-ink-3">
                            {Number.isFinite(margin) ? `MRG ${usd(margin)}` : "MRG —"}
                          </span>
                        </div>
                      </Cell>
                    );
                  })}
                  <Cell align="right">
                    <div className="flex flex-col">
                      <span className="text-ink-3">{num(floor, 2)} bps</span>
                      <span className="text-[10px] text-ink-3">statutory floor</span>
                    </div>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Card
          title="Equity & Underwater"
          subtitle="bucketed NAV with peak-to-trough drawdown"
          icon="backtest"
          actions={
            <Badge tone="neutral" mono>
              {text(asRecord(performance).bucket, "1D")}
            </Badge>
          }
        >
          {loading && equity.length === 0 ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading equity curve
            </div>
          ) : (
            <EquityDrawdownCurve equity={equity} drawdown={drawdown} height={216} />
          )}
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-hairline pt-2">
            <KeyValue
              columns={2}
              items={[
                { label: "Sharpe", value: num(asRecord(performance).sharpe, 2) },
                { label: "Sortino", value: num(asRecord(performance).sortino, 2) },
                { label: "Profit factor", value: num(asRecord(performance).profit_factor, 2) },
                { label: "Win rate", value: pct(asRecord(performance).win_rate_pct, 2) },
              ]}
            />
          </div>
        </Card>

        <div className="flex min-w-0 flex-col gap-3">
          <Card
            title="23-Gate Preflight"
            subtitle="live-cutover certificate"
            icon="verification"
            actions={
              <Badge
                tone={preflightSummary.ready ? "bull" : preflightSummary.failed > 0 ? "bear" : "muted"}
                mono
              >
                {int(preflightSummary.passed)}/{int(preflightSummary.total)}
              </Badge>
            }
          >
            <ProgressBar
              value={preflightSummary.passed}
              max={preflightSummary.total > 0 ? preflightSummary.total : 23}
              tone={preflightSummary.failed > 0 ? "bear" : "bull"}
              height={6}
              label={`${int(preflightSummary.failed)} failed · ${text(preflightSummary.release, "schema unreported")}`}
            />
            <div className="mt-2 flex flex-wrap gap-1">
              {preflightSummary.gates.slice(0, 23).map((gate, index) => (
                <Badge
                  key={`${gate.id}-${index}`}
                  tone={
                    gate.status.includes("PASS") ? "bull" : gate.status.includes("FAIL") ? "bear" : "muted"
                  }
                >
                  {index + 1}
                </Badge>
              ))}
            </div>
            <div className="mt-2 border-t border-hairline pt-2">
              <KeyValue
                columns={2}
                items={[
                  {
                    label: "Supervisor",
                    value: supervisor.attached === true ? "ATTACHED" : "SINGLE PROCESS",
                  },
                  { label: "Running", value: supervisor.running === true ? "YES" : "NO" },
                  { label: "Feed mode", value: text(supervisor.feed_mode) },
                  { label: "Silo", value: text(supervisor.silo) },
                ]}
              />
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.9fr)]">
        <Card
          title="Active Missions"
          subtitle="target pacing"
          icon="target"
          actions={
            <Badge tone="neutral" mono>
              {missionRows.length}
            </Badge>
          }
        >
          {loading && missionRows.length === 0 ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading missions
            </div>
          ) : missionRows.length === 0 ? (
            <EmptyState
              title="No missions"
              hint="Approve a mission to activate tranche pacing and the profit-split saga."
            />
          ) : (
            <div className="flex flex-col gap-2">
              {missionRows.slice(0, 5).map((mission, index) => {
                const pacing = asRecord(mission.pacing);
                const progress = safeNumber(pacing.progress, safeNumber(mission.progress, 0));
                return (
                  <div
                    key={`${text(mission.mission_id, "mission")}-${index}`}
                    className="rounded-[4px] border border-hairline bg-canvas p-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-[11px] font-semibold uppercase tracking-[0.1em] text-ink">
                        {text(mission.mission_id)}
                      </span>
                      <Badge tone={mission.approval_state === "REVOKE" ? "bear" : "accent"} mono>
                        {text(mission.approval_state, "NONE")}
                      </Badge>
                    </div>
                    <div className="mt-1.5">
                      <ProgressBar
                        value={progress * 100}
                        max={100}
                        tone={progress >= 1 ? "bull" : "accent"}
                        label={`${pct(progress * 100, 1)} · target ${usd(mission.target_micros)} · ${num(mission.days_remaining, 1)}d left`}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card
          title="Ring 3 Stream"
          subtitle="12 typed frames · multiplexed, sequenced, SHA-256 sealed"
          icon="activity"
          actions={
            <Badge tone={stream.status === "open" ? "bull" : "muted"} mono>
              {stream.frames.length} FRAMES
            </Badge>
          }
        >
          <div className="mb-2 flex flex-wrap gap-1">
            {SSE_TYPES.map((type) => (
              <Badge key={type} tone={streamTone(type)} mono>
                {type} {counts[type] ?? 0}
              </Badge>
            ))}
          </div>
          {stream.error !== null ? <Banner tone="bear">{stream.error}</Banner> : null}
          {stream.frames.length === 0 ? (
            <EmptyState
              title="Awaiting frames"
              hint="The stream reconnects with jittered backoff; frames appear as the gateway multiplexes them."
            />
          ) : (
            <Table columns={["TYPE", "TOPIC", "WHEN", "PAYLOAD"]} dense>
              {[...stream.frames]
                .reverse()
                .slice(0, 14)
                .map((frame) => (
                  <Row key={frame.id}>
                    <Cell>
                      <Badge tone={streamTone(frame.type)} mono>
                        {frame.type}
                      </Badge>
                    </Cell>
                    <Cell mono>{frame.topic}</Cell>
                    <Cell mono align="right">
                      {clockNs(frame.receivedAt * 1_000_000)}
                    </Cell>
                    <Cell className="max-w-[360px] truncate font-mono text-[10px] text-ink-3">
                      {framePreview(frame.data)}
                    </Cell>
                  </Row>
                ))}
            </Table>
          )}
        </Card>
      </div>
    </div>
  );
}

export { DashboardPage };
