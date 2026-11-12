import type {
  BrokerControlPayload,
  BrokerCredentialPayload,
  BrokerCredentialReport,
  CockpitState,
  ExtensionsActionResponse,
  ExtensionsControlPayload,
  ExtensionsState,
  FeedControlResponse,
  FeedMode,
  FormalProofState,
  KillReport,
  LLMControlPayload,
  LLMProbeReport,
  MetricsSnapshot,
  RearmReport,
  SupervisorControlResponse,
  VaultRotationReport,
  VaultStatus,
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

/* ------------------------------------------------------------------ *
 * Phase 3 Step 2 · feed hot-swap, write-only broker vault, LLM probe
 * ------------------------------------------------------------------ */

/**
 * Hot-swap the market-data path without restarting any worker.
 *
 * `LIVE_PUBLIC_DUAL_VENDOR` runs one immediate four-silo dual-vendor poll, so
 * the response carries the per-silo receipt rather than a promise.
 */
export function postFeedMode(
  feedMode: FeedMode,
  ingestCycle = true
): Promise<FeedControlResponse> {
  return request<FeedControlResponse>('/api/v2/control/feeds', {
    method: 'POST',
    body: JSON.stringify({
      feed_mode: feedMode,
      ingest_cycle: ingestCycle,
      operator: 'ring3.cockpit',
    }),
  })
}

/**
 * Store a testnet / sandbox venue credential in Ring 0.
 *
 * The API key and secret are write-only: the response carries only `***last4`
 * plus the dry-run Ed25519 handshake receipt.
 */
export function postBrokerCredentials(
  payload: BrokerCredentialPayload
): Promise<BrokerCredentialReport> {
  return request<BrokerCredentialReport>('/api/v2/control/brokers/credentials', {
    method: 'POST',
    body: JSON.stringify({ sandbox: true, operator: 'ring3.cockpit', ...payload }),
  })
}

/** Probe every configured LLM endpoint under the strict 250 ms hard cap. */
export function postLLMProbe(): Promise<LLMProbeReport> {
  return request<LLMProbeReport>('/api/v2/control/llms/probe', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit' }),
  })
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 3 — 4-ring OS supervisor control
 * ------------------------------------------------------------------ */

/**
 * Start / stop / crash-restart the four OS worker processes.
 *
 * `restart_ring` needs `ringIndex`; the response carries the previous and the
 * new PID so the cockpit can prove the worker really was replaced.
 */
export function postSupervisor(
  action: 'start' | 'stop' | 'restart_ring' | 'status',
  ringIndex?: number
): Promise<SupervisorControlResponse> {
  return request<SupervisorControlResponse>('/api/v2/control/supervisor', {
    method: 'POST',
    body: JSON.stringify({
      action,
      ring_index: ringIndex ?? null,
      operator: 'ring3.cockpit',
    }),
  })
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 4 — Prometheus / OpenTelemetry metrics exporter
 * ------------------------------------------------------------------ */

/** Standard Prometheus / OpenMetrics scrape target (text exposition). */
export function metricsUrl(): string {
  return `${API_BASE}/api/v2/metrics`
}

/** Alias scrape path (`/metrics`) for a stock Prometheus job config. */
export function metricsAliasUrl(): string {
  return `${API_BASE}/metrics`
}

/** JSON telemetry consumed by the live cockpit memory/telemetry chip. */
export function metricsJsonUrl(): string {
  return `${API_BASE}/api/v2/metrics/json`
}

export function fetchMetrics(): Promise<MetricsSnapshot> {
  return request<MetricsSnapshot>('/api/v2/metrics/json')
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 5 — Ring 0 zero-downtime key rotation + OS tuning
 * ------------------------------------------------------------------ */

/** Current key version, archived Ed25519 keys and the OS tuning summary. */
export function fetchVaultStatus(): Promise<VaultStatus> {
  return request<VaultStatus>('/api/v2/control/vault/status')
}

/**
 * Rotate the Ring 0 Fernet master keys and Ed25519 order-signing keypair.
 *
 * Zero downtime and zero credential loss: stored LLM and broker secrets are
 * re-encrypted in place and the previous public key is archived so historical
 * signed orders still verify. The reply is proof-only — never secret material.
 */
export function postVaultRotate(
  operator = 'ring3.cockpit'
): Promise<VaultRotationReport> {
  return request<VaultRotationReport>('/api/v2/control/vault/rotate-keys', {
    method: 'POST',
    body: JSON.stringify({ operator, reason: 'operator-initiated rotation' }),
  })
}
