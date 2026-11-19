/**
 * NIOS V2.2 SSE multiplexer client.
 *
 * `GET /api/v2/stream` is consumed with a fetch reader (not EventSource) so the
 * `X-NIOS-Session` header and the `?topic=` / `?silo=` projections can be
 * forwarded, and so an `AbortController` gives us deterministic unmount
 * cleanup.  All twelve Ring 3 frame types are recognised.
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

function buildUrl(options: StreamOptions): string {
  const params = new URLSearchParams();
  if (options.topics && options.topics.length > 0) params.set("topic", options.topics.join(","));
  if (options.silo) params.set("silo", options.silo);
  const query = params.toString();
  return `/api/v2/stream${query.length > 0 ? `?${query}` : ""}`;
}

/**
 * Open the multiplexed SSE stream.  Returns a disposer that aborts the socket.
 * The caller owns reconnection policy.
 */
export function openStream(options: StreamOptions): () => void {
  const controller = new AbortController();
  const url = buildUrl(options);
  let closed = false;
  let sequence = 0;
  let buffer = "";
  let pendingType = "MESSAGE";
  let pendingId = "";
  let pendingData: string[] = [];

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
    sequence += 1;
    const frame: StreamFrame = {
      id: sequence,
      type: type.length > 0 ? type.toUpperCase() : "MESSAGE",
      topic: text(data.topic ?? data.channel ?? id, "stream"),
      receivedAt: Date.now(),
      data,
    };
    options.onFrame?.(frame);
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
  };

  void run();

  return () => {
    closed = true;
    controller.abort();
    options.onStatus?.("closed", null);
  };
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
