import { type ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  type AlertRecord,
  type SessionState,
  errorMessage,
  fetchAlerts,
  fetchSession,
  isCriticalAlert,
} from "../lib/api";

interface SessionContextValue {
  session: SessionState | null;
  loading: boolean;
  error: string | null;
  lastSync: number;
  alerts: AlertRecord[];
  unacknowledged: number;
  critical: number;
  refresh: () => Promise<void>;
  refreshAlerts: () => Promise<void>;
  adoptSession: (next: SessionState | null) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState(0);
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchSession();
      setSession(next);
      setError(null);
      setLastSync(Date.now());
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshAlerts = useCallback(async () => {
    try {
      const next = await fetchAlerts(100);
      setAlerts(next);
    } catch {
      /* alert centre is best-effort; the shell must never break on it */
    }
  }, []);

  const adoptSession = useCallback((next: SessionState | null) => {
    setSession(next);
    setLoading(false);
    setLastSync(Date.now());
  }, []);

  useEffect(() => {
    void refresh();
    void refreshAlerts();
    const sessionTimer = globalThis.setInterval(() => void refresh(), 20_000);
    const alertTimer = globalThis.setInterval(() => void refreshAlerts(), 15_000);
    return () => {
      globalThis.clearInterval(sessionTimer);
      globalThis.clearInterval(alertTimer);
    };
  }, [refresh, refreshAlerts]);

  const value = useMemo<SessionContextValue>(() => {
    const unacknowledged = alerts.filter((alert) => !alert.acknowledged).length;
    const critical = alerts.filter((alert) => !alert.acknowledged && isCriticalAlert(alert)).length;
    return {
      session,
      loading,
      error,
      lastSync,
      alerts,
      unacknowledged,
      critical,
      refresh,
      refreshAlerts,
      adoptSession,
    };
  }, [adoptSession, alerts, error, lastSync, loading, refresh, refreshAlerts, session]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) throw new Error("useSession must be used inside <SessionProvider>");
  return context;
}

/** Re-render every `ms` so session TTLs and clocks stay live. */
export function useTicker(ms = 1000): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    const timer = globalThis.setInterval(() => setTick(Date.now()), ms);
    return () => globalThis.clearInterval(timer);
  }, [ms]);
  return tick;
}

/** Remaining TTL in seconds, recomputed from `expires_at_utc` when present. */
export function sessionTtl(session: SessionState | null, nowMs: number): number {
  if (!session) return 0;
  if (session.expiresAtUtc) {
    const parsed = new Date(session.expiresAtUtc).getTime();
    if (Number.isFinite(parsed)) return Math.max(0, Math.floor((parsed - nowMs) / 1000));
  }
  return Math.max(0, Math.floor(session.ttlRemainingS));
}

export function useSessionTtl(): number {
  const { session } = useSession();
  const now = useTicker(1000);
  return sessionTtl(session, now);
}
