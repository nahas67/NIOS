import { useCallback, useEffect, useState } from 'react'

import {
  authApi,
  type AuthSession,
  type AuthStatus,
  setStoredToken,
  storedToken,
} from '@/lib/auth'

export interface AuthController {
  status: AuthStatus | null
  error: string | null
  busy: boolean
  refresh: () => Promise<void>
  applySession: (session: AuthSession) => Promise<void>
  signOut: () => Promise<void>
}

/** Shared auth state for the router-level guards and the workspace top bar. */
export function useAuth(): AuthController {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const next = await authApi.status()
      setStatus(next)
      setError(null)
    } catch (cause) {
      setStatus(null)
      setError(cause instanceof Error ? cause.message : 'gateway unreachable')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const applySession = useCallback(
    async (session: AuthSession) => {
      setStoredToken(session.token)
      await refresh()
    },
    [refresh],
  )

  const signOut = useCallback(async () => {
    setBusy(true)
    try {
      await authApi.logout()
    } catch {
      // a revoked/expired token is already logged out
    } finally {
      setStoredToken(null)
      setBusy(false)
      await refresh()
    }
  }, [refresh])

  return { status, error, busy, refresh, applySession, signOut }
}

export function hasStoredToken(): boolean {
  return storedToken() !== null
}
