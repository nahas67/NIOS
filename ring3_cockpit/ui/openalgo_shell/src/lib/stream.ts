/**
 * NIOS V2.2 SSE multiplexer client.
 *
 * `GET /api/v2/stream` is consumed with a fetch reader (not EventSource) so the
 * `X-NIOS-Session` header and the `?topic=` / `?silo=` projections can be
 * forwarded, and so an `AbortController` gives us deterministic unmount
 * cleanup.  All twelve Ring 3 frame types are recognised.
 *
 * Market frames (`TICK` / `DEPTH`) are coalesced: only the newest value per
 * instrument survives inside one animation frame (hard 100 ms fallback), so a
 * tick storm can never allocate an unbounded frame backlog.  State-critical
 * frames are never buffered and are always dispatched after the buffered
 * market tail, so a halt is never read behind a stale quote.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getSessionToken } from "./api";
import { asRecord, safeNumber, text } from "./format";

export const SSE_TYPES = [
  "TICK",
  "DEPTH",
  "ORDER",
  "HEARTBEAT",
  "SIGNAL",
  "MISSION_PROGRESS",
  "TRANCHE_HALT",
  "WALLET_SWEEP",
  "CONCEPT_DRIFT_ALERT",
  "COLD_START_RECON",
  "POST_MORTEM_COMPILED",
  "SYSTEM_ALERT",
] as const;

export type SseType = (typeof SSE_TYPES)[number];

export const SSE_TYPE_TONE: Record<string, "neutral" | "accent" | "bull" | "bear" | "muted"> = {
  TICK: "muted",
  DEPTH: "muted",
  ORDER: "accent",
  HEARTBEAT: "muted",
  SIGNAL: "accent",
  MISSION_PROGRESS: "bull",
  TRANCHE_HALT: "bear",
  WALLET_SWEEP: "accent",
  CONCEPT_DRIFT_ALERT: "bear",
  COLD_START_RECON: "accent",
  POST_MORTEM_COMPILED: "neutral",
  SYSTEM_ALERT: "bear",
};

export interface StreamFrame {
  id: number;
  type: string;
  topic: string;
  receivedAt: number;
  data: Record<string, unknown>;
}

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "error" | "closed";

export interface StreamOptions {
  topics?: string[];
  silo?: string | null;
  onFrame?: (frame: StreamFrame) => void;
  onStatus?: (status: StreamStatus, error: string | null) => void;
}

/**
 * Market-data frame types.  They arrive at tick rate and every consumer only
 * renders the newest value per instrument, so they are safe to coalesce.  Both
 * the terminal vocabulary (`TICK` / `DEPTH`) and the gateway vocabulary
 * (`TICK_UPDATE` / `L2_DEPTH_DELTA`) are recognised.
 */
export const MARKET_FRAME_TYPES: ReadonlySet<string> = new Set([
  "TICK",
  "TICK_UPDATE",
  "DEPTH",
  "DEPTH_DELTA",
  "L2_DEPTH_DELTA",
]);

/**
 * State-critical frames.  They carry order, tranche, wallet, reconciliation or
 * alert state and must never be buffered, collapsed or reordered behind a
 * quote.
 */
export const STATE_CRITICAL_TYPES: ReadonlySet<string> = new Set([
  "ORDER",
  "ORDER_LIFECYCLE",
  "TRANCHE_HALT",
  "WALLET_SWEEP",
  "COLD_START_RECON",
  "SYSTEM_ALERT",
]);

/** Hard ceiling on the coalescing window when `requestAnimationFrame` cannot run. */
export const COALESCE_FALLBACK_MS = 100;

/** Upper bound on buffered instrument keys; the buffer is LRU-evicted beyond this. */
const MAX_PENDING_FRAMES = 1_024;

function frameKey(frame: StreamFrame): string {
  const identity = text(
    frame.data.symbol ?? frame.data.instrument_id ?? frame.data.instrument ?? frame.data.key ?? frame.topic,
    "",
  );
  return `${frame.type}|${identity}`;
}

function canAnimate(): boolean {
  if (typeof globalThis.requestAnimationFrame !== "function") return false;
  if (typeof document === "undefined") return true;
  return document.visibilityState !== "hidden";
}

export interface FrameCoalescer {
  /** Buffer a market frame, or dispatch an immediate frame ahead of the buffer. */
  push: (frame: StreamFrame) => void;
  /** Deliver everything buffered right now and keep accepting frames. */
  flushNow: () => void;
  /** Drop the buffer and cancel the rAF / fallback timer.  Terminal. */
  dispose: () => void;
}

/**
 * Bounded frame coalescer.
 *
 * High-frequency market frames are merged into a `Map` keyed by frame type and
 * instrument, so peak memory is `O(instruments)` rather than `O(ticks)`.  The
 * flush is driven by `requestAnimationFrame` with an unconditional `fallbackMs`
 * timer, which also covers hidden tabs (where rAF never fires) and runtimes
 * without rAF.  Any non-market frame flushes the buffer first, so delivery
 * order always matches arrival order at the control-frame boundaries.
 */
export function createFrameCoalescer(
  deliver: (frame: StreamFrame) => void,
  fallbackMs = COALESCE_FALLBACK_MS,
): FrameCoalescer {
  const pending = new Map<string, StreamFrame>();
  let rafHandle: number | null = null;
  let timerHandle: ReturnType<typeof globalThis.setTimeout> | null = null;
  let disposed = false;

  const cancel = () => {
    if (rafHandle !== null) {
      if (typeof globalThis.cancelAnimationFrame === "function") globalThis.cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
    if (timerHandle !== null) {
      globalThis.clearTimeout(timerHandle);
      timerHandle = null;
    }
  };

  const flushNow = () => {
    cancel();
    if (disposed || pending.size === 0) return;
    const batch = [...pending.values()];
    pending.clear();
    for (const frame of batch) deliver(frame);
  };

  const schedule = () => {
    if (disposed) return;
    if (timerHandle === null) timerHandle = globalThis.setTimeout(flushNow, fallbackMs);
    if (rafHandle === null && canAnimate()) rafHandle = globalThis.requestAnimationFrame(flushNow);
  };

  const push = (frame: StreamFrame) => {
    if (disposed) return;
    const coalescable = MARKET_FRAME_TYPES.has(frame.type) && !STATE_CRITICAL_TYPES.has(frame.type);
    if (!coalescable) {
      // Zero buffering for control frames, and the market tail is flushed ahead
      // of them so a halt can never be read behind a stale quote.
      flushNow();
      deliver(frame);
      return;
    }
    const key = frameKey(frame);
    pending.delete(key);
    pending.set(key, frame);
    if (pending.size > MAX_PENDING_FRAMES) {
      const oldest = pending.keys().next();
      if (!oldest.done) pending.delete(oldest.value);
    }
    schedule();
  };

  const dispose = () => {
    disposed = true;
    cancel();
    pending.clear();
  };

  return { push, flushNow, dispose };
}

export interface StreamHandle {
  (): void;
  /** Deliver any buffered market frames immediately; the stream stays open. */
  flushNow: () => void;
}

function buildUrl(options: StreamOptions): string {
  const params = new URLSearchParams();
  if (options.topics && options.topics.length > 0) params.set("topic", options.topics.join(","));
  if (options.silo) params.set("silo", options.silo);
  const query = params.toString();
  return `/api/v2/stream${query.length > 0 ? `?${query}` : ""}`;
}

/**
 * Open the multiplexed SSE stream.  Returns a disposer that aborts the socket
 * and drops any buffered market frame.  The caller owns reconnection policy.
 */
export function openStream(options: StreamOptions): StreamHandle {
  const controller = new AbortController();
  const url = buildUrl(options);
  let closed = false;
  let sequence = 0;
  let buffer = "";
  let pendingType = "MESSAGE";
  let pendingId = "";
  let pendingData: string[] = [];

  const coalescer = createFrameCoalescer((frame) => {
    if (closed) return;
    sequence += 1;
    frame.id = sequence;
    options.onFrame?.(frame);
  });

  const emit = (type: string, id: string, payload: string) => {
    let data: Record<string, unknown> = {};
    if (payload.length > 0) {
      try {
        const parsed: unknown = JSON.parse(payload);
        data = asRecord(parsed);
      } catch {
        data = { raw: payload };
      }
    }
    coalescer.push({
      id: 0,
      type: type.length > 0 ? type.toUpperCase() : "MESSAGE",
      topic: text(data.topic ?? data.channel ?? id, "stream"),
      receivedAt: Date.now(),
      data,
    });
  };

  const dispatchLine = (line: string) => {
    if (line.length === 0) {
      if (pendingData.length > 0) emit(pendingType, pendingId, pendingData.join("\n"));
      pendingType = "MESSAGE";
      pendingId = "";
      pendingData = [];
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") pendingType = value;
    else if (field === "id") pendingId = value;
    else if (field === "data") pendingData.push(value);
  };

  const run = async () => {
    options.onStatus?.("connecting", null);
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    const token = getSessionToken();
    if (token) headers["X-NIOS-Session"] = token;

    let response: Response;
    try {
      response = await fetch(url, { headers, credentials: "include", cache: "no-store", signal: controller.signal });
    } catch (error) {
      if (!closed) options.onStatus?.("error", String(error));
      return;
    }

    if (!response.ok || !response.body) {
      if (!closed) options.onStatus?.("error", `SSE HTTP ${response.status}`);
      return;
    }

    options.onStatus?.("open", null);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newline = buffer.indexOf("\n");
        while (newline !== -1) {
          const line = buffer.slice(0, newline).replace(/\r$/, "");
          buffer = buffer.slice(newline + 1);
          dispatchLine(line);
          newline = buffer.indexOf("\n");
        }
      }
    } catch (error) {
      if (!closed && !controller.signal.aborted) options.onStatus?.("error", String(error));
    }

    coalescer.flushNow();
  };

  void run();

  const dispose = () => {
    if (closed) return;
    closed = true;
    coalescer.dispose();
    controller.abort();
    options.onStatus?.("closed", null);
  };

  return Object.assign(dispose, { flushNow: () => coalescer.flushNow() });
}

export interface UseMarketStreamResult {
  frames: StreamFrame[];
  status: StreamStatus;
  error: string | null;
  reconnectAttempts: number;
  clear: () => void;
  lastByType: Record<string, StreamFrame>;
}

/** Subscribe to the Ring 3 stream with bounded retention and jittered retry. */
export function useMarketStream(options: { topics?: string[]; silo?: string | null; max?: number } = {}): UseMarketStreamResult {
  const max = options.max ?? 120;
  const topicKey = (options.topics ?? []).join(",");
  const silo = options.silo ?? "";
  const [frames, setFrames] = useState<StreamFrame[]>([]);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = useCallback(() => setFrames([]), []);

  useEffect(() => {
    let disposed = false;
    let disposeStream: (() => void) | null = null;
    let attempt = 0;

    const connect = () => {
      if (disposed) return;
      disposeStream = openStream({
        topics: topicKey.length > 0 ? topicKey.split(",") : undefined,
        silo: silo.length > 0 ? silo : null,
        onFrame: (frame) => {
          setFrames((previous) => {
            const next = [...previous, frame];
            return next.length > max ? next.slice(next.length - max) : next;
          });
        },
        onStatus: (next, detail) => {
          if (disposed) return;
          setStatus(next);
          setError(detail);
          if (next === "error" || next === "closed") {
            attempt += 1;
            setReconnectAttempts(attempt);
            const delay = Math.min(15_000, 750 * 2 ** Math.min(attempt, 5));
            if (!disposed) retryRef.current = globalThis.setTimeout(connect, delay);
          }
        },
      });
    };

    connect();

    return () => {
      disposed = true;
      if (retryRef.current !== null) globalThis.clearTimeout(retryRef.current);
      disposeStream?.();
    };
  }, [max, silo, topicKey]);

  const lastByType: Record<string, StreamFrame> = {};
  for (const frame of frames) lastByType[frame.type] = frame;

  return { frames, status, error, reconnectAttempts, clear, lastByType };
}

export function frameCounts(frames: StreamFrame[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const frame of frames) counts[frame.type] = (counts[frame.type] ?? 0) + 1;
  return counts;
}

export function framePrice(frame: StreamFrame | undefined): number {
  if (!frame) return Number.NaN;
  return safeNumber(frame.data.price_micros ?? frame.data.price ?? frame.data.last, Number.NaN);
}
