/**
 * V1.1 sovereign operator auth + dual workspace client.
 *
 * The session token is returned in the body (and mirrored into an HttpOnly
 * cookie by the gateway).  We also keep it in `sessionStorage` so an XHR can
 * carry `X-NIOS-Session` explicitly when cookie policy blocks third-party
 * storage — the gateway accepts either.
 */

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
const TOKEN_KEY = 'nios.session.v1'

export type AuthTier = 'NONE' | 'PAPER' | 'LIVE'
export type WorkspaceMode = 'paper' | 'live'

export interface AuthStatus {
  initialized: boolean
  authenticated: boolean
  username: string | null
  tier: AuthTier
  clean_slate_active: boolean
}

export interface AuthSession {
  ok: boolean
  token: string
  username: string
  tier: AuthTier
  issued_ns: number
  expires_ns: number
  clean_slate?: boolean
  preflight_ok?: boolean
  warnings?: string[]
}

export interface VaultBalance {
  micros: number
  nav: number
}

export interface PaperSiloState {
  vaults: Record<string, VaultBalance> & { nav_micros?: number; nav?: number }
  nav_micros: number
  mode?: string
}

export interface PaperWorkspaceState {
  workspace: 'paper'
  mode: 'PAPER'
  tier: AuthTier
  username: string | null
  clean_slate_active: boolean
  silos: Record<string, PaperSiloState>
  orders: {
    working: unknown[]
    events: unknown[]
    counts: Record<string, number>
  }
  ledger: {
    vaults: Record<string, unknown>
    double_entry: Record<string, number>
    counts: Record<string, number>
  }
  shadow: {
    shadow_fills: number
    required_fills: number
    remaining_fills: number
    graduated: boolean
    live_fills: number
  }
  autopilot: { enabled: boolean; paused?: boolean; ticks?: number }
  token_spend_usd: number
  generated_at_ns: number
}

export interface LiveWorkspaceState {
  workspace: 'live'
  mode: 'LIVE'
  tier: AuthTier
  username: string | null
  silos: Record<string, { mode?: string; broker?: string; configured?: boolean; frozen?: boolean }>
  broker_credentials: Record<string, unknown>
  preflight: Record<string, unknown>
  feed: Record<string, unknown>
  testnet: Record<string, unknown>
  kill_switch: Record<string, unknown>
  live_fills: number
  generated_at_ns: number
}

export class AuthError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'AuthError'
    this.status = status
  }
}

export function storedToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setStoredToken(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token)
    else window.sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // storage disabled: the HttpOnly cookie is still used
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = storedToken()
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { 'X-NIOS-Session': token } : {}),
      ...(init?.headers ?? {}),
    },
  })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // keep the raw body
    }
    throw new AuthError(detail || `HTTP ${response.status}`, response.status)
  }
  return (text ? JSON.parse(text) : {}) as T
}

export const authApi = {
  status: () => request<AuthStatus>('/api/v2/auth/status'),

  setup: (payload: { username: string; password: string; live_pin: string }) =>
    request<AuthSession>('/api/v2/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ ...payload, start_clean_slate: true }),
    }),

  login: (payload: { username: string; password: string }) =>
    request<AuthSession>('/api/v2/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  elevate: (live_pin: string) =>
    request<AuthSession>('/api/v2/auth/elevate-live', {
      method: 'POST',
      body: JSON.stringify({ live_pin }),
    }),

  logout: () => request<{ ok: boolean; revoked: boolean }>('/api/v2/auth/logout', { method: 'POST' }),

  resetEverything: (payload: { password: string; live_pin: string }) =>
    request<Record<string, unknown>>('/api/v2/auth/reset', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  resetWorkspace: () =>
    request<Record<string, unknown>>('/api/v2/control/workspace/reset', { method: 'POST' }),

  fundPaper: (payload: { silo: string; amount_units: number; vault?: string }) =>
    request<Record<string, unknown>>('/api/v2/control/ledger/fund-paper', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  setAutopilot: (enabled: boolean) =>
    request<{ ok: boolean; autopilot: { enabled: boolean } }>(
      '/api/v2/control/workspace/autopilot',
      { method: 'POST', body: JSON.stringify({ enabled }) },
    ),

  paperState: () => request<PaperWorkspaceState>('/api/v2/workspace/paper/state'),

  liveState: () => request<LiveWorkspaceState>('/api/v2/workspace/live/state'),

  armSilo: (payload: { silo: string; mode: 'LIVE' | 'PAPER'; confirm_live: boolean }) =>
    request<Record<string, unknown>>('/api/v2/control/brokers', {
      method: 'POST',
      body: JSON.stringify({ ...payload, operator: 'ring3.operator' }),
    }),

  flatten: (reason = 'operator emergency flatten') =>
    request<Record<string, unknown>>('/api/v2/control/killswitch', {
      method: 'POST',
      body: JSON.stringify({ reason, flatten: true, confirm: true }),
    }),

  rearm: () =>
    request<Record<string, unknown>>('/api/v2/control/rearm', {
      method: 'POST',
      body: JSON.stringify({ reason: 'operator re-arm' }),
    }),
}
