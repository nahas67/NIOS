/**
 * V2.3 gateway telemetry reader — six measured fields, zero fabrication.
 *
 * The gateway measures real durations on the real hot paths (`core/telemetry.py`)
 * and publishes them as:
 *
 *  - `latency_ms`            — Ring 3 request round trip
 *  - `ring0_p99_ms` … `ring3_p99_ms` — per-ring p99 latency
 *  - `feed_lag_ms` + `feed_lag_status` — age of the newest venue tick
 *  - `pending_intents_count` — unresolved outbox intents + sweeps + unknown orders
 *  - `adwin_drift`           — `{state, drift_score, window_size, last_checked_utc}`
 *  - `iv_rank_bps`           — deterministic 0…10000 IV rank
 *
 * This module normalises them once for every surface (shell strip, dashboard,
 * brain, forge, options).  A field renders `—` only when the backend sent
 * `null` (or has not shipped the field yet); nothing here ever invents a value.
 */

import { useEffect, useState } from "react";
import { ApiError, api } from "./api";
import { asRecord, num, int } from "./format";

export interface AdwinDrift {
  state: string | null;
  driftScore: number | null;
  windowSize: number | null;
  lastCheckedUtc: string | null;
}

export interface GatewayTelemetry {
  latencyMs: number | null;
  ring0P99Ms: number | null;
  ring1P99Ms: number | null;
  ring2P99Ms: number | null;
  ring3P99Ms: number | null;
  feedLagMs: number | null;
  feedLagStatus: string | null;
  pendingIntents: number | null;
  adwin: AdwinDrift | null;
  ivRankBps: number | null;
}

export const EMPTY_TELEMETRY: GatewayTelemetry = {
  latencyMs: null,
  ring0P99Ms: null,
  ring1P99Ms: null,
  ring2P99Ms: null,
  ring3P99Ms: null,
  feedLagMs: null,
  feedLagStatus: null,
  pendingIntents: null,
  adwin: null,
  ivRankBps: null,
};

function finiteOrNull(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function textOrNull(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** `adwin_drift` is an object on V2.3 and a bare score on older payloads. */
export function adwinOf(value: unknown): AdwinDrift | null {
  if (value === null || value === undefined) return null;
  const scalar = finiteOrNull(value);
  if (scalar !== null) {
    return { state: null, driftScore: scalar, windowSize: null, lastCheckedUtc: null };
  }
  const record = asRecord(value);
  if (Object.keys(record).length === 0) return null;
  return {
    state: textOrNull(record.state ?? record.adwin_state ?? record.status),
    driftScore: finiteOrNull(record.drift_score ?? record.score ?? record.drift),
    windowSize: finiteOrNull(record.window_size ?? record.window ?? record.samples),
    lastCheckedUtc: textOrNull(record.last_checked_utc ?? record.last_checked ?? record.checked_at_utc),
  };
}

/** Field-level merge for drift readings: the first non-null value wins. */
export function mergeDrift(...sources: Array<AdwinDrift | null | undefined>): AdwinDrift | null {
  const present = sources.filter((source): source is AdwinDrift => source !== null && source !== undefined);
  if (present.length === 0) return null;
  const merged: AdwinDrift = { ...present[0] };
  for (const source of present.slice(1)) {
    merged.state ??= source.state;
    merged.driftScore ??= source.driftScore;
    merged.windowSize ??= source.windowSize;
    merged.lastCheckedUtc ??= source.lastCheckedUtc;
  }
  return merged;
}

function readRecord(record: Record<string, unknown>): GatewayTelemetry {
  return {
    latencyMs: finiteOrNull(record.latency_ms),
    ring0P99Ms: finiteOrNull(record.ring0_p99_ms),
    ring1P99Ms: finiteOrNull(record.ring1_p99_ms),
    ring2P99Ms: finiteOrNull(record.ring2_p99_ms),
    ring3P99Ms: finiteOrNull(record.ring3_p99_ms),
    feedLagMs: finiteOrNull(record.feed_lag_ms),
    feedLagStatus: textOrNull(record.feed_lag_status),
    pendingIntents: finiteOrNull(
      record.pending_intents_count ?? record.pending_intents ?? record.pending_intent_count,
    ),
    adwin: adwinOf(record.adwin_drift),
    ivRankBps: finiteOrNull(record.iv_rank_bps),
  };
}

/** Field-level merge: the first non-null value wins, left to right. */
export function mergeTelemetry(
  ...sources: Array<GatewayTelemetry | null | undefined>
): GatewayTelemetry | null {
  const present = sources.filter(
    (source): source is GatewayTelemetry => source !== null && source !== undefined,
  );
  if (present.length === 0) return null;
  const merged: GatewayTelemetry = { ...present[0] };
  for (const source of present.slice(1)) {
    merged.latencyMs ??= source.latencyMs;
    merged.ring0P99Ms ??= source.ring0P99Ms;
    merged.ring1P99Ms ??= source.ring1P99Ms;
    merged.ring2P99Ms ??= source.ring2P99Ms;
    merged.ring3P99Ms ??= source.ring3P99Ms;
    merged.feedLagMs ??= source.feedLagMs;
    merged.feedLagStatus ??= source.feedLagStatus;
    merged.pendingIntents ??= source.pendingIntents;
    merged.adwin ??= source.adwin;
    merged.ivRankBps ??= source.ivRankBps;
  }
  return merged;
}

const NESTED_KEYS = [
  "telemetry",
  "gateway_telemetry",
  "gateway",
  "latency",
  "risk_telemetry",
  "payload",
  "status",
] as const;

/** Pull every telemetry field present anywhere in a gateway payload. */
export function telemetryOf(payload: unknown): GatewayTelemetry {
  if (payload === null || payload === undefined) return { ...EMPTY_TELEMETRY };
  let out = readRecord(asRecord(payload));
  for (const key of NESTED_KEYS) {
    const nested = asRecord(asRecord(payload)[key]);
    if (Object.keys(nested).length === 0) continue;
    out = mergeTelemetry(out, readRecord(nested)) ?? out;
  }
  return out;
}

// ---------------------------------------------------------------------------
// shared poll: one interval for the whole terminal, ref-counted by subscribers
// ---------------------------------------------------------------------------

const STATUS_PATH = "/status";
const POLL_MS = 10_000;

let snapshot: GatewayTelemetry | null = null;
const listeners = new Set<(value: GatewayTelemetry | null) => void>();
let pollTimer: ReturnType<typeof setInterval> | null = null;
let subscribers = 0;
let inFlight = false;
let statusMounted: boolean | null = null;

function publish(next: GatewayTelemetry | null): void {
  snapshot = next;
  for (const listener of listeners) listener(next);
}

async function readStatus(): Promise<void> {
  if (inFlight || statusMounted === false) return;
  inFlight = true;
  try {
    const payload = await api.get<unknown>(STATUS_PATH);
    statusMounted = true;
    publish(telemetryOf(payload));
  } catch (error) {
    // A gateway without `/api/v2/status` must not spam 404s or the console.
    if (error instanceof ApiError && error.status === 404) statusMounted = false;
  } finally {
    inFlight = false;
  }
}

function startPolling(): void {
  if (pollTimer !== null) return;
  void readStatus();
  pollTimer = globalThis.setInterval(() => void readStatus(), POLL_MS);
}

function stopPolling(): void {
  if (pollTimer === null) return;
  globalThis.clearInterval(pollTimer);
  pollTimer = null;
}

/**
 * Optional aggregate surface (`GET /api/v2/cockpit/overview`).
 *
 * The V2.3 gateway lane mounts it; an older gateway 404s exactly once and is
 * never polled again, so the dashboard never spams the network or the console.
 */
let overviewMounted: boolean | null = null;

export async function fetchCockpitOverview(): Promise<unknown | null> {
  if (overviewMounted === false) return null;
  try {
    const payload = await api.get<unknown>("/cockpit/overview");
    overviewMounted = true;
    return payload;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) overviewMounted = false;
    return null;
  }
}

/** Subscribe to the shared gateway telemetry poll. */
export function useGatewayTelemetry(): GatewayTelemetry | null {
  const [value, setValue] = useState<GatewayTelemetry | null>(snapshot);
  useEffect(() => {
    subscribers += 1;
    listeners.add(setValue);
    setValue(snapshot);
    startPolling();
    return () => {
      subscribers -= 1;
      listeners.delete(setValue);
      if (subscribers <= 0) stopPolling();
    };
  }, []);
  return value;
}

// ---------------------------------------------------------------------------
// formatting — `—` is reserved for "the backend sent null"
// ---------------------------------------------------------------------------

export function msText(value: number | null | undefined, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? `${num(value, digits)} ms` : "—";
}

export function countText(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? int(value) : "—";
}

export function bpsCountText(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${int(value)} bps` : "—";
}

/** IV rank bps (0…10000) → the 0…100 percentile the desk quotes. */
export function rankFromBps(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value / 100 : null;
}

export function feedTone(status: string | null): "bull" | "bear" | "accent" | "muted" {
  const token = (status ?? "").toUpperCase();
  if (token.includes("STALE") || token.includes("LAG") || token.includes("DOWN")) return "bear";
  if (token.includes("IDLE") || token.includes("WARM")) return "accent";
  if (token.includes("LIVE") || token.includes("OK") || token.includes("FRESH")) return "bull";
  return "muted";
}

export function driftTone(state: string | null): "bull" | "bear" | "accent" | "muted" {
  const token = (state ?? "").toUpperCase();
  if (token.includes("HALVED") || token.includes("DRIFT")) return "bear";
  if (token.includes("PAUSED")) return "accent";
  if (token.includes("STABLE")) return "bull";
  return "muted";
}

export function presenceTone(value: number | null | undefined): "neutral" | "muted" {
  return typeof value === "number" && Number.isFinite(value) ? "neutral" : "muted";
}
