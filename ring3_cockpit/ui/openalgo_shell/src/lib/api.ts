import type {
  AgentAssignPayload,
  AgentAssignReceipt,
  AgentCancelReceipt,
  AgentsPageData,
  AnalyzerPageData,
  BacktestPageData,
  BacktestReceipt,
  BacktestRunPayload,
  BrokerControlPayload,
  BrokerCredentialPayload,
  BrokerCredentialReport,
  CancelOrdersPayload,
  CancelOrdersReceipt,
  ChallengerEvalReceipt,
  ChaosDrillReceipt,
  CockpitState,
  CommitteeDebateReceipt,
  ExtensionsActionResponse,
  ExtensionsControlPayload,
  ExtensionsState,
  FeedControlResponse,
  FeedMode,
  ForgeHedgeReceipt,
  ForgePageData,
  FormalProofState,
  InteractiveQuantScan,
  KillReport,
  LedgerPageData,
  LedgerReconcileReceipt,
  LLMControlPayload,
  LLMProbeReport,
  McpPageData,
  MetricsSnapshot,
  MiroFishShockReceipt,
  NurseryPromotionReceipt,
  OptionPricePayload,
  OptionPriceReceipt,
  OrdersPageData,
  OsTuneReceipt,
  PagesIndex,
  PaperOrderPayload,
  PaperOrderReceipt,
  PreflightCertificate,
  QuantPageData,
  QuantScanPayload,
  RearmReport,
  ShadowBurstPayload,
  ShadowBurstReceipt,
  SkillMarkdownReceipt,
  SupervisorControlResponse,
  VaultRotationReport,
  VaultStatus,
  VoprCampaignReceipt,
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

/* ------------------------------------------------------------------ *
 * Phase 4 Step 2 — 22-gate pre-flight go-live certificate
 * ------------------------------------------------------------------ */

/**
 * Interactive readiness certificate (`GET /api/v2/control/preflight`).
 *
 * The server replays a short-lived in-process certificate and, by default, the
 * persisted 22-row smoke receipt for this commit, so the cockpit stays
 * responsive. `refresh` forces a fresh certification run.
 */
export function fetchPreflight(refresh = false): Promise<PreflightCertificate> {
  const query = refresh ? '?refresh=true' : ''
  return request<PreflightCertificate>(`/api/v2/control/preflight${query}`)
}

/**
 * Full 22-gate certification (`POST /api/v2/control/preflight`).
 *
 * `fullSmoke=true` re-spawns the 22-row suite instead of replaying the receipt —
 * the release-gate form an operator runs before `confirm_live=true`.
 */
export function postPreflight(
  fullSmoke = false,
  silo = 'ALL'
): Promise<PreflightCertificate> {
  return request<PreflightCertificate>('/api/v2/control/preflight', {
    method: 'POST',
    body: JSON.stringify({ full_smoke: fullSmoke, silo, refresh: true }),
  })
}

/* ------------------------------------------------------------------ *
 * Phase 4 Step 2 — cross-process forge + tail-hedge cycle
 * ------------------------------------------------------------------ */

/**
 * Run one closed-loop Nightly Alpha Forge + Tactical Tail-Hedger cycle.
 *
 * Ring 2 demotes decayed champions (disarming their leases), breeds challengers,
 * runs the contagion cascade and prices a grounded put; Ring 0 verifies and
 * Ed25519-signs it. The receipt is proof-only.
 */
export function postSupervisorForgeHedge(): Promise<ForgeHedgeReceipt> {
  return request<ForgeHedgeReceipt>('/api/v2/control/supervisor/forge-hedge', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit' }),
  })
}

/* ------------------------------------------------------------------ *
 * Step 4 — read-only multi-page cockpit telemetry
 * ------------------------------------------------------------------ */

/** Page catalog: eleven production pages, their routes and live counts. */
export function fetchPages(): Promise<PagesIndex> {
  return request<PagesIndex>('/api/v2/cockpit/pages')
}

/** SmartOMS blotter, Maker-Peg / TWAP slicers, atomic outbox and positions. */
export function fetchOrdersPage(): Promise<OrdersPageData> {
  return request<OrdersPageData>('/api/v2/cockpit/orders')
}

/** 30-fill L2 shadow graduation nursery plus adverse-selection telemetry. */
export function fetchAnalyzerPage(): Promise<AnalyzerPageData> {
  return request<AnalyzerPageData>('/api/v2/cockpit/analyzer')
}

/** 490-factor scanner, valuation lab, tail-hedger and loader readiness. */
export function fetchQuantPage(refresh = false): Promise<QuantPageData> {
  const query = refresh ? '?refresh=true' : ''
  return request<QuantPageData>(`/api/v2/cockpit/quant${query}`)
}

/** Nocturnal Alpha Forge leaderboard, challenger plugins and skill catalog. */
export function fetchForgePage(): Promise<ForgePageData> {
  return request<ForgePageData>('/api/v2/cockpit/forge')
}

/** Double-entry IBOR journal, 35/15 ratchet preview and backend schema status. */
export function fetchLedgerPage(): Promise<LedgerPageData> {
  return request<LedgerPageData>('/api/v2/cockpit/ledger')
}

/** Air-gapped MCP tool inventory and its read-only invariants. */
export interface McpCallResult {
  name: string
  ok: boolean
  result?: unknown
  error?: string
  elapsed_ms: number
}

/**
 * Invoke one read-only MCP tool through the air-gapped Ring 3 bridge.
 *
 * The bridge rejects any tool that could read broker keys or sign an order, so a
 * successful receipt is proof-only telemetry.
 */
export function callMcpTool(
  name: string,
  args: Record<string, unknown> = {}
): Promise<McpCallResult> {
  return request<McpCallResult>('/api/v2/mcp/call', {
    method: 'POST',
    body: JSON.stringify({ name, arguments: args }),
  })
}

export function fetchMcpPage(): Promise<McpPageData> {
  return request<McpPageData>('/api/v2/cockpit/mcp')
}

/* ------------------------------------------------------------------ *
 * Phase 4 Step 5 — interactive operator actions (paper-only)
 * ------------------------------------------------------------------ */

/**
 * Submit a grounded PAPER order through the atomic outbox, Smart OMS and IBOR.
 *
 * The endpoint refuses with 409 when the global kill latch is HALTED or the
 * silo is frozen, so a click can never bypass the four-ring safety envelope.
 * A filled order applies the 35% / 15% / 50% in-silo ratchet on the receipt.
 */
export function postPaperOrder(payload: PaperOrderPayload): Promise<PaperOrderReceipt> {
  return request<PaperOrderReceipt>('/api/v2/control/orders/paper', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Cancel one working order, or every open order when `order_id` is `ALL`.
 *
 * Pending outbox reservations are voided with the same atomic call so the
 * blotter and the outbox ledger never disagree after a cancel.
 */
export function postCancelOrders(
  payload: CancelOrdersPayload = {}
): Promise<CancelOrdersReceipt> {
  return request<CancelOrdersReceipt>('/api/v2/control/orders/cancel', {
    method: 'POST',
    body: JSON.stringify({ order_id: 'ALL', operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Drive N shadow execution ticks through the L2 twin for one silo.
 *
 * One tick does not always resolve a fill (the twin posts a maker peg, then
 * fills on a later tick), so the gateway keeps ticking until the requested
 * fills land or the ceiling is reached.
 */
export function postShadowBurst(payload: ShadowBurstPayload): Promise<ShadowBurstReceipt> {
  return request<ShadowBurstReceipt>('/api/v2/control/analyzer/shadow-burst', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Price a European option and seal every input and Greek into a SHA-256
 * `GroundedFactPacket` verified by the numerical grounding verifier.
 */
export function postPriceOption(payload: OptionPricePayload): Promise<OptionPriceReceipt> {
  return request<OptionPriceReceipt>('/api/v2/control/quant/price-option', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/** Re-run the 490-factor vectorized scan and return the fresh ranking. */
export function postQuantScan(payload: QuantScanPayload = { top_k: 15 }): Promise<InteractiveQuantScan> {
  return request<InteractiveQuantScan>('/api/v2/control/quant/scan', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Load one full `SKILL.md` on demand (progressive disclosure, sec_016).
 *
 * The gateway only reads inside the skill registry root and answers 404 for an
 * unknown or path-traversing name.
 */
export function fetchSkillMarkdown(name: string): Promise<SkillMarkdownReceipt> {
  return request<SkillMarkdownReceipt>(
    `/api/v2/cockpit/skills/${encodeURIComponent(name)}`
  )
}

/** Reconcile all four silos and prove `sum(debits) == sum(credits)`. */
export function postLedgerReconcile(
  silos?: string[]
): Promise<LedgerReconcileReceipt> {
  return request<LedgerReconcileReceipt>('/api/v2/control/ledger/reconcile', {
    method: 'POST',
    body: JSON.stringify({ silos: silos ?? null, operator: 'ring3.cockpit' }),
  })
}

/* ------------------------------------------------------------------ *
 * Phase 4 Step 6 — /verification, /brain and /forge operator actions
 * ------------------------------------------------------------------ */

/**
 * SIGKILL a worker mid-outbox and prove every ``sec_009`` reboot invariant.
 *
 * The drill replays the journal and returns `pending_after == 0`,
 * `duplicate_transfer_ids == 0`, a stale lease swept to `DISARMED` and
 * `sum(debits) == sum(credits)` — never a promise that they should hold.
 */
export function postChaosDrill(payload: {
  iterations?: number
  hold_ms?: number
  seed?: number
  silo?: string
  symbol?: string
}): Promise<ChaosDrillReceipt> {
  return request<ChaosDrillReceipt>('/api/v2/control/verification/chaos-drill', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Run the seeded deterministic VOPR fault campaign.
 *
 * Same seed + same step count always reproduce the same fault histogram, so a
 * red row is a reproducible defect rather than a flake.
 */
export function postVoprCampaign(payload: {
  seed?: number
  steps?: number
}): Promise<VoprCampaignReceipt> {
  return request<VoprCampaignReceipt>('/api/v2/control/verification/vopr', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Apply and audit OS / socket low-latency tuning.
 *
 * Rows the serving process may not change stay `RECOMMENDED` with their exact
 * command; they are reported, never silently skipped.
 */
export function postOsTune(payload: {
  apply?: boolean
  ring?: string
}): Promise<OsTuneReceipt> {
  return request<OsTuneReceipt>('/api/v2/control/verification/os-tune', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Simulate one five-archetype, three-tick MiroFish contagion cascade.
 *
 * Deterministic for a given shock vector: the returned SHA-256 digest commits
 * to the inputs and outputs, never to wall-clock jitter.
 */
export function postMiroFishShock(payload: {
  symbol?: string
  shock_bps?: number
  sentiment_tag?: string
  volatility_ratio?: number
  spread_bps?: number
  gamma_flip_proximity?: number
  silo?: string
}): Promise<MiroFishShockReceipt> {
  return request<MiroFishShockReceipt>('/api/v2/control/brain/mirofish-shock', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Run one grounded System 2 committee debate and seal the transcript.
 *
 * Every numerical claim is re-computed by `NumericalGroundingVerifier`; a
 * fabricated figure aborts the debate with `UNGROUNDED-NUMERICAL-CLAIM`.
 */
export function postCommitteeDebate(payload: {
  silo?: string
  symbol?: string
  spread_bps?: number
}): Promise<CommitteeDebateReceipt> {
  return request<CommitteeDebateReceipt>('/api/v2/control/brain/committee-debate', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

/**
 * Evaluate a challenger genome/plugin against the forge nursery gates.
 *
 * Promotion requires `Deflated Sharpe >= 1.50` **and** `>= 30` shadow fills,
 * both read from the forge — never from the request.
 */
export function postEvaluateChallenger(payload: {
  plugin_name: string
  candidate_sharpe?: number
  shadow_fills?: number
  silo?: string
}): Promise<ChallengerEvalReceipt> {
  return request<ChallengerEvalReceipt>('/api/v2/control/forge/evaluate-challenger', {
    method: 'POST',
    body: JSON.stringify({ operator: 'ring3.cockpit', ...payload }),
  })
}

// ---------------------------------------------------------------------------
// V1.2 - agent task assignment workstation (/agents)
// ---------------------------------------------------------------------------

/** Read the six specialist agents, eight strategies and the task queue. */
export function fetchAgentsPage(): Promise<AgentsPageData> {
  return request<AgentsPageData>('/api/v2/cockpit/agents')
}

/**
 * Assign one grounded task to a Ring 2 specialist agent.
 *
 * Ring 2 emits research only: the receipt carries a SHA-256 `grounding_sha256`
 * and never an order, a broker key, or an IBOR write.
 */
export function postAgentTask(payload: AgentAssignPayload): Promise<AgentAssignReceipt> {
  return request<AgentAssignReceipt>('/api/v2/control/agents/assign', {
    method: 'POST',
    body: JSON.stringify({ ...payload }),
  })
}

/** Read one task together with its full grounded markdown report. */
export function fetchAgentTask(taskId: string): Promise<AgentAssignReceipt> {
  return request<AgentAssignReceipt>(
    `/api/v2/control/agents/tasks/${encodeURIComponent(taskId)}`,
  )
}

/** Cancel one task, or pass `ALL` to clear the whole queue. */
export function postCancelAgentTasks(taskId = 'ALL'): Promise<AgentCancelReceipt> {
  return request<AgentCancelReceipt>('/api/v2/control/agents/cancel', {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId }),
  })
}

// ---------------------------------------------------------------------------
// V1.2 - walk-forward backtesting workstation (/backtest)
// ---------------------------------------------------------------------------

/** Read the strategy catalog, silo cost-floor table and saved runs. */
export function fetchBacktestPage(): Promise<BacktestPageData> {
  return request<BacktestPageData>('/api/v2/cockpit/backtest')
}

/**
 * Run a deterministic tax-net walk-forward backtest.
 *
 * Every trade is charged the silo's statutory floor (India STT is 5.50 bps of
 * the 8.50 bps total) before the Deflated Sharpe gate is computed.
 */
export function postRunBacktest(payload: BacktestRunPayload): Promise<BacktestReceipt> {
  return request<BacktestReceipt>('/api/v2/control/backtest/run', {
    method: 'POST',
    body: JSON.stringify({ ...payload }),
  })
}

/**
 * Promote a passing run into the 30-fill shadow nursery.
 *
 * Rejected with HTTP 409 unless the Deflated Sharpe clears 1.50 and the tax-net
 * edge clears +0.50 bps. The nursery never starts above 0/30 fills.
 */
export function postPromoteToNursery(backtestId: string): Promise<NurseryPromotionReceipt> {
  return request<NurseryPromotionReceipt>(
    '/api/v2/control/backtest/promote-to-nursery',
    { method: 'POST', body: JSON.stringify({ backtest_id: backtestId }) },
  )
}
