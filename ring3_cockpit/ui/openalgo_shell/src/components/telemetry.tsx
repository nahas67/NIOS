/**
 * Flat matte telemetry surfaces (V2.3).
 *
 * Every figure is a real measured gateway value; `tabular-nums` keeps the tape
 * from jittering and `—` appears only when the backend explicitly sent `null`.
 * Zero gradients, zero shadows, zero blur, zero pills.
 */

import { Icon } from "./Icon";
import { Badge, Stat, StatGrid } from "./ui";
import { timeUtc } from "../lib/format";
import {
  type AdwinDrift,
  type GatewayTelemetry,
  bpsCountText,
  countText,
  driftTone,
  feedTone,
  msText,
  presenceTone,
  rankFromBps,
  useGatewayTelemetry,
} from "../lib/telemetry";

const RING_CELLS = [
  { key: "ring0P99Ms", label: "RING 0 p99", hint: "firewall · OMS sign · IBOR" },
  { key: "ring1P99Ms", label: "RING 1 p99", hint: "SHM · IPC · hot path" },
  { key: "ring2P99Ms", label: "RING 2 p99", hint: "agent + quant evaluation" },
  { key: "ring3P99Ms", label: "RING 3 p99", hint: "HTTP middleware" },
] as const;

/** `LATENCY` · `FEED LAG` · `PENDING INTENTS` — the three gateway-wide counters. */
export function GatewayHealthStats({
  telemetry,
  columns = 3,
}: {
  telemetry: GatewayTelemetry | null;
  columns?: number;
}) {
  const lagStatus = telemetry?.feedLagStatus ?? null;
  return (
    <StatGrid columns={columns}>
      <Stat
        label="LATENCY"
        value={msText(telemetry?.latencyMs)}
        hint="measured request round trip"
        tone={presenceTone(telemetry?.latencyMs)}
      />
      <Stat
        label="FEED LAG"
        value={msText(telemetry?.feedLagMs)}
        hint={lagStatus === null ? "no venue tick measured yet" : `${lagStatus} · newest tick`}
        tone={feedTone(lagStatus)}
      />
      <Stat
        label="PENDING INTENTS"
        value={countText(telemetry?.pendingIntents)}
        hint="outbox · sweeps · unknown orders"
        tone={presenceTone(telemetry?.pendingIntents)}
      />
    </StatGrid>
  );
}

/** Per-ring `p99` cells straight off the gateway latency tracker. */
export function RingP99Stats({
  telemetry,
  columns = 4,
}: {
  telemetry: GatewayTelemetry | null;
  columns?: number;
}) {
  return (
    <StatGrid columns={columns}>
      {RING_CELLS.map((ring) => {
        const value = telemetry?.[ring.key] ?? null;
        return (
          <Stat
            key={ring.key}
            label={ring.label}
            value={msText(value, 3)}
            hint={ring.hint}
            tone={presenceTone(value)}
          />
        );
      })}
    </StatGrid>
  );
}

/** One ADWIN row: state badge, drift score, window size, last check. */
export function AdwinDriftRow({ drift, label }: { drift: AdwinDrift | null; label: string }) {
  const state = drift?.state ?? null;
  const score = drift?.driftScore ?? null;
  const window = drift?.windowSize ?? null;
  const checked = drift?.lastCheckedUtc ?? null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline/60 pb-1 last:border-b-0">
      <span className="min-w-0 truncate text-[11px] text-ink-2">{label}</span>
      <div className="flex shrink-0 items-center gap-2 text-[10px] tabular-nums text-ink-3">
        <Badge tone={driftTone(state)} mono>
          {state ?? "NO STATE"}
        </Badge>
        <span className="text-ink-2">{score === null ? "—" : score.toFixed(3)}</span>
        <span>{window === null ? "WIN —" : `WIN ${Math.trunc(window).toLocaleString("en-US")}`}</span>
        <span>{checked === null ? "—" : timeUtc(checked)}</span>
      </div>
    </div>
  );
}

/**
 * `IV RANK` in basis points (0…10000). Falls back to the 0…100 percentile only
 * when the chain payload carries the legacy `iv_rank` field instead.
 */
export function IvRankStat({
  ivRankBps,
  legacyPercentile,
}: {
  ivRankBps: number | null;
  legacyPercentile: number | null;
}) {
  const hasBps = typeof ivRankBps === "number" && Number.isFinite(ivRankBps);
  const raw = hasBps ? rankFromBps(ivRankBps) : legacyPercentile;
  const percentile = typeof raw === "number" && Number.isFinite(raw) ? raw : null;
  return (
    <Stat
      label="IV RANK"
      value={hasBps ? bpsCountText(ivRankBps) : percentile === null ? "—" : percentile.toFixed(1)}
      hint={percentile === null ? "not in payload" : `percentile ${percentile.toFixed(1)} / 100`}
      tone={percentile !== null && percentile > 70 ? "bear" : "neutral"}
    />
  );
}

function StripItem({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-center gap-1 text-[9px] uppercase tracking-[0.1em] text-ink-3">
      <span>{label}</span>
      <span className="font-semibold tabular-nums text-ink-2">{value}</span>
    </span>
  );
}

/**
 * Compact gateway strip that rides under the shell top bar on both workspaces.
 * One shared poll serves the whole terminal.
 */
export function TelemetryStrip() {
  const telemetry = useGatewayTelemetry();
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-hairline bg-surface px-3 py-1">
      <span className="flex items-center gap-1 text-[9px] uppercase tracking-[0.14em] text-ink-3">
        <Icon name="activity" size={11} />
        GATEWAY
      </span>
      <StripItem label="LATENCY" value={msText(telemetry?.latencyMs)} />
      <StripItem label="FEED LAG" value={msText(telemetry?.feedLagMs)} />
      <StripItem label="PENDING INTENTS" value={countText(telemetry?.pendingIntents)} />
      <span className="flex items-center gap-1 text-[9px] uppercase tracking-[0.1em] text-ink-3">
        <span>RING 1 p99</span>
        <span className="font-semibold tabular-nums text-ink-2">{msText(telemetry?.ring1P99Ms, 3)}</span>
      </span>
    </div>
  );
}
