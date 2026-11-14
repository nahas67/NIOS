/**
 * Hydration hooks.
 *
 * `useResource` owns the loading/error/refresh lifecycle for one read model and
 * keeps the fetcher in a ref so a re-render never restarts the request. The
 * interval poll is opt-in and always cleared on unmount.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface ResourceState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "request failed";
}

export function useResource<T>(
  key: string,
  load: () => Promise<T>,
  intervalMs = 0,
): ResourceState<T> {
  const loadRef = useRef(load);
  loadRef.current = load;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    setLoading(true);
    void loadRef
      .current()
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setLoading(false);
      })
      .catch((problem: unknown) => {
        if (cancelled) return;
        setError(messageOf(problem));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [key, tick]);
  useEffect(() => {
    if (intervalMs <= 0) return;
    const id = window.setInterval(() => setTick((value) => value + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, key]);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  return { data, error, loading, reload };
}

export interface ActionState<T> {
  busy: boolean;
  receipt: T | null;
  error: string | null;
  run: (task: () => Promise<T>) => Promise<T | null>;
  reset: () => void;
}

/** Run one operator mutation with a single-flight guard and a receipt slot. */
export function useAction<T = unknown>(): ActionState<T> {
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (task: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setError(null);
    try {
      const result = await task();
      setReceipt(result);
      return result;
    } catch (problem: unknown) {
      setError(messageOf(problem));
      return null;
    } finally {
      setBusy(false);
    }
  }, []);

  const reset = useCallback(() => {
    setReceipt(null);
    setError(null);
  }, []);

  return { busy, receipt, error, run, reset };
}

/** A ticking clock used by the live session TTL and relative timestamps. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
