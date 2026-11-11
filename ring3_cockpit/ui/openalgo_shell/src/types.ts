export type VaultKey =
  | 'ReserveYield'
  | 'LongTermCompounder'
  | 'TacticalSwing'
  | 'SpinalScalper'

export type VaultBalances = Record<VaultKey, { micros: number; nav: number }> & {
  nav_micros: number
  nav: number
}

export type SiloId = 'CRYPTO_BINANCE' | 'INDIA_ZERODHA_DHAN' | 'US_IBKR' | 'FX_MT5'

export type SystemStatus = 'ARMED' | 'HALTED'

export interface SystemState {
  status: SystemStatus
  kill_latch_hot: boolean
  halt_reason: string
  timestamp_ns: number
}

export interface AbstentionState {
  target_rate: number
  rate: number
  abstentions: number
  ticks: number
  trades: number
  shadow_fills: number
  breaker_trips: number
  ring0_rejects: number
}

export interface CostFloor {
  spread_bps: number
  maker_fee_bps: number
  taker_fee_bps: number
  statutory_tax_bps: number
  fee_stt_bps: number
  round_trip_cost_bps: number
  source: string
}

export interface AdapterProbe {
  name: string
  status: 'READY' | 'HARVESTED_NOT_IMPORTABLE' | 'MISSING'
  ready: boolean
  importable: boolean
  harvested: boolean
  reason: string
}

export interface BrokerState {
  silo_id: SiloId
  market: string
  label: string
  venue: string
  mode: 'PAPER' | 'LIVE'
  venue_ready: boolean
  trading_state: 'ACTIVE' | 'REDUCING' | 'HALTED'
  frozen: boolean
  freeze_reason: string
  cost_floor: CostFloor
  max_daily_loss_micros: number
  realized_pnl_micros: number
  daily_loss_exceeded: boolean
  allocation: Record<VaultKey, number>
  adapters: AdapterProbe[]
  ready_adapters: string[]
  updated_at_ns: number
}

export interface SiloState extends BrokerState {
  vaults: VaultBalances
}

export interface LeaseRow {
  symbol: string
  mode: 'DISARMED' | 'LONG_ONLY' | 'SHORT_ONLY' | 'MEAN_REV'
  target_bps: number
  stop_bps: number
  expires_at_ns: number
  remaining_ns: number
  ttl_seconds: number
  ttl_pct: number
  strategy_id_hash: number
}

export interface Archetype {
  name: string
  weight: number
  members?: string[]
}

export interface GroundingLedger {
  entries: number
  verified: boolean
  latest_digest: string | null
}

export interface ContagionState {
  available: boolean
  symbol: string
  crowd_asymmetry_score: number
  cascade_fragility_index: number
  narrative_exhaustion_divergence: number
  requires_ring1_halt: boolean
  archetype_count: number
  archetypes: Archetype[]
  archetype_groups: Archetype[]
  fragility_halt_threshold: number
  tick_flows: number[]
  latency_ms: number
  revoked_long_leases: number
  as_of_ns?: number
  grounding_ledger: GroundingLedger
}

export interface LLMSlot {
  provider: string
  model: string
  base_url: string
  daily_spend_cap_usd: number
  spend_usd: number
  budget_remaining_usd: number
  timeout_ms: number
  api_key_masked: string | null
  api_key_set: boolean
  api_key_write_only: boolean
  last_latency_ms: number
  circuit_open: boolean
  updated_at_ns: number
}

export interface LLMState {
  daily_spend_cap_usd: number
  daily_spend_usd: number
  budget_remaining_usd: number
  write_only_credentials: boolean
  system1: LLMSlot
  system2: LLMSlot
}

export interface CockpitCard {
  silo: string
  symbol: string
  mode_badge: string
  story_sentence: string
  gross_bps: number
  fee_stt_bps: number
  uncert_q98_bps: number
  net_lb_bps: number
  timestamp_ns: number
  proof_line: string
  sequence?: number
}

export interface KillReport {
  status: string
  reason: string
  latency_ns: number
  latency_ms: number
  budget_ns: number
  within_budget: boolean
  kill_latch_hot: boolean
  disarmed_leases: number
  cancelled_orders: number
  voided_reservations: number
  flattened_orders: number
  pending_outbox_after: number
  timestamp_ns: number
}

export interface RearmReport {
  status: string
  reason: string
  operator: string
  kill_latch_hot: boolean
  rearmed_silos: string[]
  leases_cleared: boolean
  autopilot_resumes: boolean
  timestamp_ns: number
}

export interface AutopilotState {
  enabled: boolean
  mode?: string
  interval_ms?: number
  instruments?: Record<string, string>
  rounds?: number
  ticks?: number
  live_fills?: number
  shadow_fills?: number
  graduated?: boolean
  paused?: boolean
  last_tick_ns?: number
  last?: {
    status?: string
    actions?: Record<string, string>
    prices?: Record<string, number>
    cascade_fragility_index?: number
  }
}

export interface SkillInfo {
  name: string
  description: string
  silo: string
  tags: string[]
  path: string
  enabled: boolean
  body_loaded: boolean
  body_chars: number
  body_tokens_estimate: number
  frontmatter_tokens_estimate: number
  load_count: number
}

export interface SkillCatalog {
  count: number
  skills: SkillInfo[]
  bodies_loaded: number
  loaded_tokens_estimate: number
  token_overhead: number
  progressive: boolean
  load_order: string[]
  enabled_count: number
}

export interface PluginInfo {
  name: string
  kind: 'factor_pack' | 'data_loader' | 'broker_silo'
  version: string
  description: string
  author: string
  ring: string
  entrypoint: string
  factor_count: number
  status: 'SHADOW' | 'ACTIVE' | 'DISABLED'
  enabled: boolean
  shadow_fills: number
  winning_shadow_fills: number
  required_shadow_fills: number
  graduated: boolean
  registered_at_ns: number
  activated_at_ns: number
  last_error: string
}

export interface PluginCatalog {
  count: number
  active_count: number
  graduated_count: number
  shadow_count: number
  plugins: PluginInfo[]
  required_shadow_fills: number
}

export interface MCPToolInfo {
  name: string
  description: string
  inputSchema: Record<string, unknown>
  read_only: boolean
  category: string
  ring: string
  air_gap: string
  enabled: boolean
}

export interface MCPAirGap {
  pass: boolean
  tool_count: number
  forbidden_matches: unknown[]
  order_signing_tools: string[]
  key_reading_tools: string[]
  read_only_tools: number
  safety_tools: number
  air_gap: string
}

export interface MCPState {
  count: number
  enabled_count: number
  tools: MCPToolInfo[]
  air_gap: MCPAirGap
  transports: string[]
}

export interface ForgeReport {
  run_at_ns: number
  retired_count: number
  promoted_count: number
  held_count: number
  genomes_evaluated: number
  fdr_rejections: number
  fdr_threshold: number
  champions_after: number
  challengers_after: number
  elapsed_ms: number
  next_run_ns: number
}

export interface ForgeState {
  schedule_utc: string
  next_run_ns: number
  last_run_ns: number
  champion_count: number
  retired_count: number
  challenger_count: number
  genome_count: number
  champions: Array<Record<string, unknown>>
  challengers: Array<Record<string, unknown>>
  last_report: ForgeReport | null
  fdr: Record<string, unknown>
  thresholds: {
    retirement_floor_bps: number
    promotion_dsr_floor: number
    min_winning_shadow_fills: number
    fdr: number
    cv: Record<string, unknown>
  }
  dataset: { periods: number; factors: number }
}

export interface ExtensionsState {
  skills: SkillCatalog
  plugins: PluginCatalog
  mcp: MCPState
  forge: ForgeState
  counts: {
    skills: number
    plugins: number
    mcp_tools: number
    champions: number
    challengers: number
  }
  air_gap: {
    ring3_read_only: boolean
    order_signing_tools: number
    key_reading_tools: number
    mcp_air_gap_pass: boolean
  }
}

export interface ExtensionsControlPayload {
  target: 'skill' | 'plugin' | 'mcp_tool' | 'forge'
  name?: string
  enabled?: boolean
  action?: 'run_audit'
  operator?: string
}

export interface ExtensionsActionResponse {
  updated: string[]
  item?: unknown
  report?: ForgeReport
  extensions: ExtensionsState
  zero_restart: boolean
}

/* ------------------------------------------------------------------ *
 * Phase 2 Step 4 · formal verification, VOPR and the options tail-hedger
 * ------------------------------------------------------------------ */

export interface TLAProofSpec {
  name: string
  states_explored: number
  transitions: number
  counterexamples: number
  violations: Array<Record<string, unknown>>
  invariants: string[]
  duration_ms: number
  terminal_states: number
  liveness_violations: number
  ok: boolean
  details: Record<string, unknown>
}

export interface TLAProofSuite {
  kind: string
  mode: string
  specs: TLAProofSpec[]
  total_states: number
  total_transitions: number
  total_counterexamples: number
  counterexamples: number
  duration_ms: number
  ok: boolean
  spec_files: Record<string, { present?: boolean; bytes?: number; sha256?: string }>
}

export interface VOPRCampaign {
  kind: string
  seed: number
  steps: number
  fault_counts: Record<string, number>
  double_entry_ok: boolean
  at_most_once_ok: boolean
  overdraft_violations: number
  conservation_violations: number
  ledger_violations: number
  duplicate_executions: number
  power_cycles: number
  reservations: number
  commits: number
  voids: number
  duration_ms: number
  ok: boolean
  errors: string[]
  details: Record<string, unknown>
}

export interface GroundedFactPacket {
  symbol: string
  valid_time_ns: number
  knowledge_ts_ns: number
  as_of_ns: number
  metrics: Record<string, number>
  sha256_digest: string
}

export interface TailHedgeGreeks {
  delta: number
  gamma: number
  vega: number
  theta: number
  rho: number
}

export interface TailHedgeProposal {
  proposal_id: string
  strategy_id: string
  silo: string
  vault: string
  symbol: string
  side: string
  qty: number
  limit_price: number
  expected_gross_bps: number
  spread_bps: number
  grounding_hash: string
  is_reduce_only: boolean
  created_at_ns: number
}

export interface TailHedgePlan {
  plan_id: string
  symbol: string
  silo: string
  vault: string
  side: string
  spot: number
  strike: number
  years: number
  volatility: number
  risk_free_rate: number
  contracts: number
  contract_multiplier: number
  premium_per_unit: number
  premium_micros: number
  premium_bps: number
  hedge_notional_micros: number
  hedge_ratio: number
  cascade_fragility_index: number
  tail_correlation: number
  greeks: TailHedgeGreeks
  packet: GroundedFactPacket
  proposal: TailHedgeProposal
  grounding_verified: boolean
  grounding_claims_checked: number
  reasons: string[]
  valuation_source: string
}

export interface TailHedgeStatus {
  triggered: boolean
  cascade_fragility_index: number
  tail_correlation: number
  reasons: string[]
  thresholds: { cascade_fragility_index: number; tail_correlation: number }
  portfolio_notional_micros: number
  tactical_vault_micros: number
  as_of_ns: number
  silos: string[]
}

export interface TailHedgerState {
  kind: string
  triggers: number
  hedges_emitted: number
  last_status: TailHedgeStatus | null
  last_plan: TailHedgePlan | null
  thresholds: {
    cascade_fragility_index: number
    tail_correlation: number
    max_premium_bps: number
  }
  valuation_donor: unknown
  contagion_attached: boolean
  copula_attached: boolean
}

export interface TigerBeetleBackendStatus {
  found?: boolean
  version?: string
  ready?: boolean
  execution_blocked?: boolean
  size_bytes?: number
  sha256?: string
}

export interface PostgresBackendStatus {
  ready?: boolean
  schema?: { valid?: boolean; tables?: string[]; checks?: number; indexes?: number }
}

export interface FormalProofState {
  kind: string
  tla: TLAProofSuite | null
  vopr: VOPRCampaign | null
  tigerbeetle: TigerBeetleBackendStatus | null
  postgres: PostgresBackendStatus | null
  tail_hedger: TailHedgerState | null
  summary: {
    states_explored: number
    counterexamples: number
    tla_ok: boolean
    vopr_ok: boolean
    tigerbeetle_ready: boolean
    postgres_ready: boolean
    tail_hedger_ready: boolean
  }
  ok: boolean
  checked_at_ns: number
}

export interface CockpitState {
  version: string
  document_id: string
  system: SystemState
  abstention: AbstentionState
  silos: Record<string, SiloState>
  brokers: { order: string[]; silos: Record<string, BrokerState> }
  leases: LeaseRow[]
  contagion: ContagionState
  llm: LLMState
  cards: CockpitCard[]
  kill_switch: KillReport | null
  extensions: ExtensionsState
  formal_proofs: FormalProofState
  autopilot: AutopilotState
  ring_processes?: RingProcessesState
  feed_mode?: FeedMode | string
  feed?: FeedState
  broker_credentials?: BrokerCredentialState
  llm_probe?: LLMProbeReport | Record<string, never>
  streams: { cockpit: string; fps: number }
}

export interface CockpitFrame {
  type: 'cockpit_frame'
  sequence: number
  timestamp_ns: number
  fps: number
  system: SystemState
  abstention: AbstentionState
  silos: Record<string, { label: string; mode: string; vaults: VaultBalances }>
  cards: CockpitCard[]
}

export interface BrokerControlPayload {
  silo: string
  mode?: 'PAPER' | 'LIVE'
  maker_fee_bps?: number
  taker_fee_bps?: number
  statutory_tax_bps?: number
  spread_bps?: number
  max_daily_loss_micros?: number
  allocation?: Record<string, number>
  confirm_live?: boolean
  operator?: string
}

export interface LLMControlPayload {
  system: 1 | 2
  provider?: string
  model?: string
  base_url?: string
  api_key?: string
  daily_spend_cap_usd?: number
  timeout_ms?: number
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 2 · feed hot-swap, broker credential vault, LLM probe
 * ------------------------------------------------------------------ */

export type FeedMode = 'PAPER_AUTOPILOT' | 'LIVE_PUBLIC_DUAL_VENDOR'

/** One ring worker inside the 4-ring OS process isolation matrix. */
export interface RingProcess {
  index: number
  worker: string
  pid: number | null
  alive: boolean
  spawned: boolean
  /** Shared-memory heartbeat age; `null` when the slot was never written. */
  heartbeat_age_ms: number | null
  heartbeat_pid?: number | null
  last_beat_ns?: number | null
  restarts: number
  cpu_core: number | null
  exit_code: number | null
}

export interface RingProcessesState {
  available: boolean
  reason?: string
  rings: RingProcess[]
  pids: Record<string, number | null>
  distinct_os_processes: number
  feed_mode?: string
  shm_prefix?: string
  heartbeat_shm?: string
  running?: boolean
}

/** Per-silo receipt for one dual-vendor public feed poll. */
export interface FeedSiloCycle {
  mode: string
  symbols: string[]
  venues: string[]
  frames: number
  accepted: number
  rejected: number
  synthetic: number
  live: number
  max_divergence_pct: number
  last_error: string
}

export interface FeedCycleReport {
  round?: number
  cycle_at_ns?: number
  frames: number
  accepted?: number
  rejected?: number
  all_live?: boolean
  silos: Record<string, FeedSiloCycle>
  skipped?: boolean
  reason?: string
}

export interface FeedState {
  feed_mode: FeedMode | string
  live_public_dual_vendor: boolean
  paper_autopilot: boolean
  available_modes: string[]
  silo_modes?: Record<string, string>
  last_cycle?: FeedCycleReport
  law?: string
}

export interface FeedControlResponse {
  feed_mode: FeedMode
  previous_feed_mode: FeedMode
  changed: boolean
  operator: string
  zero_restart: boolean
  live_public_dual_vendor: boolean
  cycle?: FeedCycleReport
  feed: FeedState
  timestamp_ns: number
}

/** One `POST /api/v2/control/supervisor` receipt. */
export interface SupervisorControlResponse {
  action: string
  changed: boolean
  running: boolean
  available?: boolean
  attached?: boolean
  distinct_os_processes: number
  four_ring_isolation?: boolean
  pids: Record<string, number | null>
  rings: RingProcess[]
  ring_index?: number
  previous_pid?: number | null
  new_pid?: number | null
  pid_changed?: boolean
  restart?: Record<string, unknown>
  stopped?: boolean
  error?: string
  operator?: string
  timestamp_ns?: number
}

/** Write-only credential payload; the secret never returns from the API. */
export interface BrokerCredentialPayload {
  silo: string
  venue: string
  api_key: string
  api_secret: string
  sandbox?: boolean
  operator?: string
}

export interface BrokerDryRun {
  signature_alg: string
  signature_hex: string
  public_key_hex: string
  payload_digest_sha256: string
  fernet_roundtrip_ok: boolean
  schema_ok: boolean
  required_fields: string[]
  payload_keys: string[]
  payload_field_count: number
  order_type: string
  instrument: string
  adapter_module: string
  adapter_ready: boolean
  post_only: boolean
  settlement_cycle: string
  elapsed_us: number
  socket_opened: boolean
  order_placed: boolean
}

export interface BrokerCredentialReport {
  silo: string
  market: string
  venue: string
  sandbox: boolean
  environment: string
  ring?: string
  vault?: string
  credential_configured: boolean
  write_only: boolean
  api_key_masked: string
  api_secret_masked: string
  masked?: { api_key: string; api_secret: string }
  dry_run_verified: boolean
  dry_run: BrokerDryRun
  stored_at_ns: number
  operator?: string
  zero_restart?: boolean
}

export interface BrokerCredentialVenueState {
  venue: string
  credential_configured: boolean
  dry_run_verified: boolean
  api_key_masked: string | null
  api_secret_masked: string | null
  environment: string | null
}

export interface BrokerCredentialSiloState {
  silo_id: string
  market: string
  venues: BrokerCredentialVenueState[]
  configured: number
  adapter_count: number
}

export interface BrokerCredentialState {
  write_only: boolean
  available?: boolean
  reason?: string
  ring?: string
  vault?: string
  venues_supported: string[]
  silos: Record<string, BrokerCredentialSiloState>
  last_verification?: Record<
    string,
    {
      venue: string
      silo: string
      dry_run_verified: boolean
      api_key_masked: string
      sandbox: boolean
      stored_at_ns: number
    }
  >
}

export interface LLMProbeModel {
  name: string
  size_bytes: number
  parameter_size: string
  quantization: string
}

export interface LLMProbeEndpoint {
  provider: string
  model: string
  base_url: string
  local: boolean
  url: string
  reachable: boolean
  latency_ms: number
  within_budget: boolean
  models: LLMProbeModel[]
  model_count: number
  error: string
}

export type LLMProbeStatus = 'LOCAL_OLLAMA_LIVE' | 'DETERMINISTIC_INSTINCT_FALLBACK'

export interface LLMProbeReport {
  status: LLMProbeStatus
  local_ollama_live: boolean
  fallback_active: boolean
  fallback_mode: string
  fallback_reason: string
  hard_timeout_ms: number
  within_budget: boolean
  circuit_breaker: {
    hard_timeout_ms: number
    tripped: boolean
    zero_tokens_on_fallback: boolean
  }
  models: LLMProbeModel[]
  model_count: number
  system1: LLMProbeEndpoint
  system2: LLMProbeEndpoint | null
  elapsed_ms: number
  probed_at_ns: number
  operator?: string
  zero_restart?: boolean
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 4 · Prometheus / OpenTelemetry metrics exporter
 * ------------------------------------------------------------------ */

export interface MetricsLatencyBucket {
  le: number
  count: number
}

export interface MetricsTickLatency {
  count: number
  sum_seconds: number
  mean_seconds: number
  buckets: MetricsLatencyBucket[]
  quantiles: Record<string, number>
  quantiles_ns: Record<string, number>
  p99_budget_seconds: number
  p99_within_budget: boolean
}

export interface MetricsRingHeartbeat {
  ring: number
  worker: string
  pid: number
  alive: boolean
  heartbeat_age_ms: number
}

export interface MetricsSiloNav {
  silo: string
  vaults: Record<VaultKey, number>
  nav_micros: number
  nav_total_micros: number
}

export interface MetricsSnapshot {
  generated_at_ns: number
  generated_at: string
  uptime_seconds: number
  node: string
  version: string
  document_id: string
  exposition: {
    prometheus: string
    alias: string
    json: string
    content_type: string
  }
  tick_latency: MetricsTickLatency
  rings: MetricsRingHeartbeat[]
  conformal: {
    strategy: string
    q98_bps: number
    effective_q98_bps: number
    samples: number
    coverage_target: number
  }
  outbox: {
    pending: number
    committed: number
    voided: number
    total: number
  }
  tokens: {
    spend_usd: number
    cap_usd: number
    remaining_usd: number
    utilization: number
  }
  leases: {
    active: number
    slots_written: number
    shm_uri: string
  }
  silos: MetricsSiloNav[]
  process: {
    rss_mb: number | null
    python_heap_mb: number | null
    threads: number | null
  }
  ring1: {
    ticks: number
    abstentions: number
    abstention_ratio: number
    shadow_fills: number
    live_proposals: number
    ring0_rejects: number
    breaker_trips: number
  }
  scrape_duration_seconds: number
}

