import type {
  BrokerControlPayload,
  CockpitState,
  ExtensionsActionResponse,
  ExtensionsControlPayload,
  ExtensionsState,
  FormalProofState,
  KillReport,
  LLMControlPayload,
  RearmReport,
} from '@/types'

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
      else if (Array.isArray(parsed.detail)) detail = JSON.stringify(parsed.detail)
    } catch {
      // keep the raw body
    }
    throw new ApiError(detail || `HTTP ${response.status}`, response.status)
  }
  return (text ? JSON.parse(text) : {}) as T
}

export function streamUrl(): string {
  return `${API_BASE}/api/v2/cockpit/stream`
}

export function fetchState(): Promise<CockpitState> {
  return request<CockpitState>('/api/v2/cockpit/state')
}

/**
 * Phase 2 Step 4 telemetry: the four TLA+ state-space proofs, the deterministic
 * 1,000-step VOPR campaign, the TigerBeetle/PostgreSQL backends and the
 * TacticalSwing options tail-hedger.
 *
 * `refresh` re-runs the exhaustive BFS and the VOPR campaign server-side; without
 * it the gateway replays its cached result so the 5 s hydration poll stays cheap.
 */
export function fetchFormalProofs(refresh = false): Promise<FormalProofState> {
  const query = refresh ? '?refresh=true' : ''
  return request<FormalProofState>(`/api/v2/cockpit/formal-proofs${query}`)
}

export function postBroker(payload: BrokerControlPayload): Promise<unknown> {
  return request('/api/v2/control/brokers', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function postLLM(payload: LLMControlPayload): Promise<unknown> {
  return request('/api/v2/control/llms', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function postKillSwitch(reason = 'operator kill switch'): Promise<KillReport> {
  return request<KillReport>('/api/v2/control/killswitch', {
    method: 'POST',
    body: JSON.stringify({ reason, flatten: true, confirm: true }),
  })
}

/**
 * Explicit human reset after a kill switch or ADR-009 vault stop.
 *
 * Clears the global kill latch, unfreezes every silo and puts them back into
 * ACTIVE trading state; the paper autopilot resumes without a restart.
 */
export function postRearm(reason = 'cockpit re-arm'): Promise<RearmReport> {
  return request<RearmReport>('/api/v2/control/rearm', {
    method: 'POST',
    body: JSON.stringify({ reason, operator: 'ring3.cockpit' }),
  })
}

export function fetchExtensions(): Promise<ExtensionsState> {
  return request<ExtensionsState>('/api/v2/control/extensions')
}

export function postExtensions(
  payload: ExtensionsControlPayload
): Promise<ExtensionsActionResponse> {
  return request<ExtensionsActionResponse>('/api/v2/control/extensions', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}
