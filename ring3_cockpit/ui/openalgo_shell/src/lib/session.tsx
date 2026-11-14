/**
 * Sovereign session context.
 *
 * Holds the authenticated tier, the workspace the portal granted and the wall
 * clock expiry of the live 30-minute lease. The token itself never lands in
 * React state: it stays in `sessionStorage` (mirrored by the gateway's HttpOnly
 * cookie) and is attached by the API client.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { authApi, setStoredToken, storedToken } from "./api";
import type { AuthSession, AuthStatus, Tier, Workspace } from "./types";

const META_KEY = "nios.session.meta.v1";

interface SessionValue {
  status: AuthStatus | null;
  session: AuthSession | null;
  tier: Tier;
  workspace: Workspace;
  username: string | null;
  loading: boolean;
  expiresAtMs: number | null;
  signIn: (session: AuthSession) => void;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

function readMeta(): AuthSession | null {
  try {
    const raw = window.sessionStorage.getItem(META_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed || typeof parsed.token !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeMeta(session: AuthSession | null): void {
  try {
    if (session) window.sessionStorage.setItem(META_KEY, JSON.stringify(session));
    else window.sessionStorage.removeItem(META_KEY);
  } catch {
    /* storage disabled */
  }
}

export function SessionProvider({ children }: { children: ReactNode }): ReactNode {
  const [session, setSession] = useState<AuthSession | null>(() => readMeta());
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const next = await authApi.status();
      setStatus(next);
      if (!next.authenticated) {
        setSession(null);
        writeMeta(null);
        setStoredToken(null);
      }
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signIn = useCallback((next: AuthSession) => {
    setStoredToken(next.token);
    writeMeta(next);
    setSession(next);
    setStatus((prev) =>
      prev
        ? {
            ...prev,
            authenticated: true,
            username: next.username,
            tier: next.tier,
            workspace: next.workspace ?? prev.workspace ?? null,
          }
        : prev,
    );
  }, []);

  const signOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* a revoked cookie is still a signed-out browser */
    }
    setStoredToken(null);
    writeMeta(null);
    setSession(null);
    setStatus((prev) => (prev ? { ...prev, authenticated: false, tier: "NONE" } : prev));
  }, []);

  const value = useMemo<SessionValue>(() => {
    const tier: Tier = session?.tier ?? status?.tier ?? "NONE";
    const expiresAtMs =
      session?.expires_ns != null ? Math.round(session.expires_ns / 1_000_000) : null;
    const workspace: Workspace =
      tier === "LIVE" ? "live" : tier === "PAPER" ? "paper" : "base";
    return {
      status,
      session,
      tier,
      workspace,
      username: session?.username ?? status?.username ?? null,
      loading,
      expiresAtMs,
      signIn,
      signOut,
      refresh,
    };
  }, [session, status, loading, signIn, signOut, refresh]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <SessionProvider>");
  return value;
}

/** True when the gateway has already minted a token in this browser. */
export function hasStoredToken(): boolean {
  return storedToken() !== null;
}
