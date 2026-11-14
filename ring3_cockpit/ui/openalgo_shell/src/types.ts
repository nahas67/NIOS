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

/* ------------------------------------------------------------------ *
 * Phase 3 Step 5 — Ring 0 zero-downtime key rotation + OS tuning
 * ------------------------------------------------------------------ */

export interface VaultOsTuningSummary {
  ok: boolean
  tuned: boolean
  platform: string
  profile: string
  applied: number
  recommended: number
  error_count: number
  socket_tcp_nodelay: number
  transports_ok: boolean
}

export interface ArchivedVaultKey {
  key_version: number
  algorithm: string
  public_key_hex: string
  fingerprint_sha256: string
  archived_at_ns: number
  read_only: boolean
  path?: string
}

export interface VaultRotationRecord {
  key_version: number
  previous_key_version: number
  rotated_at_ns: number
  operator: string
  reencrypted_llm_keys_count: number
  reencrypted_broker_venues_count: number
  reencrypted_broker_credentials_count: number
  ed25519_public_fingerprint: string
  previous_ed25519_public_fingerprint: string
}

export interface VaultStatus {
  schema: string
  status: string
  ring: string
  vault: string
  write_only: boolean
  raw_secrets_exposed: boolean
  key_version: number
  ed25519_algorithm: string
  ed25519_public_key_hex: string
  ed25519_public_fingerprint: string
  signing_key_version: number
  archived_public_keys: ArchivedVaultKey[]
  verifier_ring_read_only: boolean
  llm_keys_stored: number
  broker_credentials_stored: number
  broker_venues_stored: number
  broker_venues: string[]
  reencrypted_llm_keys_count: number
  reencrypted_broker_venues_count: number
  last_rotation: VaultRotationRecord | null
  rotation_pending_recovery: boolean
  checked_at_ns: number
  os_tuning: VaultOsTuningSummary | null
  reason?: string
}

export interface VaultRotationReport {
  schema: string
  status: string
  ring: string
  vault: string
  operator: string
  key_version: number
  previous_key_version: number
  key_version_incremented: boolean
  ed25519_algorithm: string
  ed25519_public_key_hex: string
  ed25519_public_fingerprint: string
  previous_ed25519_public_fingerprint: string
  fingerprint_changed: boolean
  reencrypted_llm_keys_count: number
  reencrypted_broker_venues_count: number
  reencrypted_broker_credentials_count: number
  broker_venues: string[]
  credentials_zero_loss: boolean
  masks_unchanged: boolean
  dry_run_verified: boolean
  fernet_roundtrip_ok: boolean
  historical_signature_verified: boolean
  historical_probe_sha256: string
  previous_public_key_archived: boolean
  archived_public_keys: ArchivedVaultKey[]
  verifier_ring: string
  zero_downtime: boolean
  zero_loss: boolean
  write_only: boolean
  raw_secrets_exposed: boolean
  rotated_at_ns: number
  elapsed_ms: number
  os_tuning: VaultOsTuningSummary | null
  zero_restart?: boolean
}


/* ------------------------------------------------------------------ *
 * Phase 4 Step 2 - 22-gate go-live pre-flight certificate
 * ------------------------------------------------------------------ */

export interface PreflightGate {
  gate: string
  label: string
  status: string
  detail: string
  evidence?: Record<string, unknown>
  duration_ms?: number
}

export interface PreflightCertificate {
  kind: string
  release: string
  schema: string
  signer: string
  silo_scope: string
  strict: boolean
  full_smoke: boolean
  ready_for_live_cutover: boolean
  passed_gates: number
  failed_gates: number
  warned_gates: number
  total_gates: number
  failed_gate_ids: string[]
  warned_gate_ids: string[]
  certificate_sha256: string
  certificate_digest_algorithm: string
  duration_ms?: number | null
  generated_at_ns?: number | null
  gates: PreflightGate[]
  gate_checklist: string[]
  served_from?: string
  cache_age_s?: number
  endpoint?: string
  next_action?: string
}

/* ------------------------------------------------------------------ *
 * Phase 4 Step 2 - closed-loop forge + Ring 0 tail-hedge receipt
 * ------------------------------------------------------------------ */

export interface ForgeHedgeSignature {
  proposal_id: string
  plan_id: string
  silo: string
  symbol: string
  vault: string
  hedge_role: string
  digest_hex: string
  signature_hex: string
  public_key_hex: string
  signature_algorithm: string
  verified: boolean
  premium_bps: number | null
  strike: number | null
  contracts: number | null
  grounding_hash: string
}

export interface ForgeHedgeReceipt {
  kind: string
  mode: string
  request_id: string
  silo: string
  symbol: string
  strategy_id: string
  champion_id: string
  champion_status?: string | null
  champion_rolling_net_bps?: number | null
  retired_champions: number
  disarmed_leases: number
  retirements: Array<Record<string, unknown>>
  promoted_champions: number
  genomes_evaluated: number
  cascade_fragility_index?: number | null
  tail_hedge_triggered: boolean
  hedge_routed: boolean
  signed: boolean
  ring0_signature: ForgeHedgeSignature | null
  rejection: { reason: string } | null
  elapsed_ms: number
}

/* ------------------------------------------------------------------ *
 * Step 4 — multi-page read-only cockpit telemetry
 * ------------------------------------------------------------------ */

export interface PageSpec {
  key: string
  title: string
  route: string
  aliases: string[]
  ring: string
  read_only: boolean
  summary: string
  live?: boolean
  status?: string
  api?: string
}

export interface PagesIndex {
  version: string
  document_id: string
  page_count: number
  pages: PageSpec[]
  routes: Record<string, string>
  counts: Record<string, number | boolean>
  air_gap: Record<string, unknown>
  generated_at_ns: number
}

export interface OrdersPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  oms: Record<string, unknown>
  working_orders: Array<Record<string, unknown>>
  twap_icebergs: Array<Record<string, unknown>>
  events: Array<Record<string, unknown>>
  outbox: Record<string, unknown>
  reservations: Array<Record<string, unknown>>
  pending_reservations: number
  positions: Record<string, unknown>
  reconciliation: Record<string, unknown>
  counts: Record<string, number>
  generated_at_ns: number
}

export interface AnalyzerStrategyRow {
  strategy_id: string
  graduated: boolean
  required_fills: number
  completed_fills: number
  remaining_fills: number
  progress_pct: number
  q98_bps: number
  adverse_selection_bps: number
  report: Record<string, unknown>
}

export interface AnalyzerNursery {
  required_fills: number
  calibrated: boolean
  graduated_count: number
  graduated_strategies: string[]
  strategies: AnalyzerStrategyRow[]
}

export interface AnalyzerPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  nursery: AnalyzerNursery
  paper: Record<string, number>
  live: Record<string, number>
  source: { package: string; components: string[] }
  generated_at_ns: number
}

export interface QuantScanCandidate {
  rank: number
  symbol: string
  score: number
  price: number
  feature_count?: number
  sha256_digest?: string
}

export interface QuantScan {
  available?: boolean
  error?: string
  candidates?: QuantScanCandidate[]
  elapsed_ms?: number
  universe_size?: number
  factor_count?: number
  as_of_ns?: number
  scan_id?: string
  kernel_count?: number
  donor_factor_count?: number
}

export interface ActivationEntryInfo {
  module: string
  ok: boolean
  error: string
  exports: string[]
}

export interface ActivationMatrix {
  ok: boolean
  import_errors: number
  loader_file_count: number
  engine_file_count: number
  loader_module_count: number
  engine_module_count: number
  total_modules: number
  registry_sources: string[]
  engine_classes: string[]
  loaders: ActivationEntryInfo[]
  engines: ActivationEntryInfo[]
  failures: string[]
}

export interface QuantPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  universe_scan: QuantScan
  factors: { count: number; kernels: number; warm: boolean }
  valuation: Record<string, unknown>
  tail_hedger: Record<string, unknown>
  datasets: ActivationMatrix
  generated_at_ns: number
}

export interface ForgeLeaderRow {
  strategy_id: string
  role: string
  deflated_sharpe: number
  net_edge_bps: number
  promotion_ready: boolean
  above_floor: boolean
  [key: string]: unknown
}

export interface ForgePageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  forge: Record<string, unknown>
  thresholds: Record<string, number>
  leaderboard: ForgeLeaderRow[]
  next_run: { seconds_until_next_run: number; next_run_ns: number; schedule_utc: string }
  plugins: Record<string, unknown>
  skills: Record<string, unknown>
  generated_at_ns: number
}

export interface LedgerPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  silos: Record<string, Record<string, unknown>>
  transfers: Array<Record<string, unknown>>
  double_entry: { debits_micros: number; credits_micros: number; balanced: boolean }
  ratchet: { long_term_pct: number; reserve_pct: number; cross_border_wire_bps: number }
  invariant: Record<string, unknown>
  backends: Record<string, unknown>
  counts: Record<string, number>
  generated_at_ns: number
}

export interface McpPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  tools: Array<Record<string, unknown>>
  count: number
  air_gap: Record<string, unknown>
  invariants: Record<string, number>
  transport: string
  console_ready: boolean
  generated_at_ns: number
}

/* ------------------------------------------------------------------ *
 * Phase 4 Step 5 — interactive operator actions (paper-only)
 * ------------------------------------------------------------------ */

export type PaperOrderStyle = 'MAKER_PEG' | 'TWAP_ICEBERG'
export type OrderSide = 'BUY' | 'SELL'

export interface PaperOrderPayload {
  silo: string
  symbol: string
  side: OrderSide
  quantity: number
  order_style: PaperOrderStyle
  limit_price?: number | null
  expected_gross_bps?: number
  spread_bps?: number
  fill_immediately?: boolean
}

export interface RatchetSplit {
  realized_net_micros: number
  long_term_micros: number
  reserve_micros: number
  scalper_micros: number
}

export interface RatchetReceipt {
  applied: boolean
  long_term_pct: number
  reserve_pct: number
  scalper_pct: number
  split_micros: RatchetSplit
  [key: string]: unknown
}

export interface PaperOrderReceipt {
  status: 'FILLED' | 'WORKING'
  mode: string
  page: string
  silo: string
  symbol: string
  side: string
  quantity: number
  order_style: string
  order_id: string
  filled: boolean
  fill_price: number | null
  notional_micros: number
  realized_net_micros: number
  ratchet: RatchetReceipt
  proposal: Record<string, unknown>
  order: Record<string, unknown> | null
  receipt: Record<string, unknown> | null
  outbox: Record<string, unknown>
  blotter: OrdersPageData
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface CancelOrdersPayload {
  order_id?: string
  silo?: string | null
  reason?: string
}

export interface CancelOrdersReceipt {
  status: string
  mode: string
  page: string
  order_id: string
  silo: string | null
  cancelled_count: number
  cancelled_order_ids: string[]
  voided_outbox_keys: string[]
  receipt: Record<string, unknown> | null
  outbox: Record<string, unknown>
  blotter: OrdersPageData
  operator: string
  reason: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface ShadowBurstPayload {
  silo: string
  rounds: number
}

export interface ShadowBurstTick {
  status: string
  ticks: number
  total_ticks: number
  shadow_fills: number
  live_fills: number
  graduated: string[]
}

export interface ShadowBurstReceipt {
  status: string
  mode: string
  page: string
  silo: string
  strategy_id: string
  rounds_requested: number
  rounds_completed: number
  ticks_executed: number
  fills_gained: number
  shadow_fills: number
  required_fills: number
  remaining_fills: number
  graduated: boolean
  graduation_threshold_met: boolean
  adverse_selection_bps: number
  q98_bps: number
  total_shadow_fills: number
  last_tick: ShadowBurstTick | null
  nursery: AnalyzerNursery
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface OptionPricePayload {
  symbol: string
  spot: number
  strike: number
  volatility: number
  days_to_expiry: number
  risk_free_rate: number
  option_type: 'PUT' | 'CALL'
}

export interface OptionGreeks {
  price: number
  delta: number
  gamma: number
  vega: number
  theta: number
  rho: number
  d1: number
  d2: number
}

export interface OptionGroundingReceipt {
  sha256_digest: string
  verified: boolean
  claims_checked: number
  derived_expressions: number
  tolerance: number
  packet: Record<string, unknown>
  report: Record<string, unknown>
}

export interface OptionPriceReceipt {
  status: string
  page: string
  symbol: string
  option_type: string
  inputs: Record<string, number>
  greeks: OptionGreeks
  grounding: OptionGroundingReceipt
  packet: Record<string, unknown>
  grounding_ledger: { packets: number }
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface QuantScanPayload {
  top_k: number
}

export interface InteractiveQuantScan {
  status: string
  page: string
  scan_id: string
  factor_count: number
  universe_size: number
  candidate_count: number
  top_k: number
  candidates: QuantScanCandidate[]
  scan_latency_ms: number
  reported_elapsed_ms: number
  kernel_count: number
  compression_ratio: number
  as_of_ns: number
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface SkillMarkdownReceipt {
  status: string
  page: string
  skill: string
  name: string
  description: string
  enabled: boolean
  path: string
  frontmatter: Record<string, unknown>
  body: string
  markdown: string
  bytes: number
  progressive_disclosure: boolean
  generated_at_ns: number
  [key: string]: unknown
}

export interface ReconcileSiloReceipt {
  silo: string
  drift_ppm: number
  drift_pct: number
  threshold_ppm: number
  within_threshold: boolean
  breached: boolean
  frozen: boolean
  symbols_checked: number
  repair_action: string
  vaults: Record<string, unknown>
  report: Record<string, unknown>
  [key: string]: unknown
}

export interface LedgerReconcileReceipt {
  status: string
  page: string
  silos_checked: string[]
  silo_count: number
  silos: Record<string, ReconcileSiloReceipt>
  breached_silos: string[]
  all_within_threshold: boolean
  threshold_ppm: number
  double_entry: {
    debits_micros: number
    credits_micros: number
    balanced: boolean
    proof: string
  }
  invariant: Record<string, unknown>
  frozen_silos: string[]
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface ChaosDrillReceipt {
  status: 'PASSED' | 'FAILED'
  page: string
  kind: string
  target: string
  silo: string
  pid: number
  killed: boolean
  returncode: number | null
  sigkill_delivered: boolean
  inflight_reservation_observed: boolean
  reservation_key: string
  iterations: number
  hold_ms: number
  seed: number
  recovery: Record<string, unknown>
  lease_reboot: {
    symbol: string
    segment: string
    stale_lease_armed: boolean
    swept: number
    active_after_reboot: number
    mode_after_reboot: number
    disarmed: boolean
    passed: boolean
  }
  invariants: {
    no_orphaned_orders: boolean
    at_most_once_execution: boolean
    stale_lease_swept_to_disarmed: boolean
    double_entry_conservation: boolean
  }
  all_invariants_hold: boolean
  proof: {
    pending_after: number
    duplicate_transfer_ids: number
    orphaned_orders: number
    sum_debits_micros: number
    sum_credits_micros: number
    lease_mode_after_reboot: number
  }
  elapsed_ms: number
  notes: string[]
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface VoprCampaignReceipt {
  status: 'PASSED' | 'FAILED'
  page: string
  kind: string
  seed: number
  steps: number
  fault_counts: Record<string, number>
  injected_faults: number
  power_cycles: number
  reservations: number
  commits: number
  voids: number
  overdrafts: number
  conservation_violations: number
  ledger_violations: number
  duplicate_executions: number
  double_entry_ok: boolean
  at_most_once_ok: boolean
  conservation_proof: {
    overdrafts: number
    double_entry_ok: boolean
    at_most_once_ok: boolean
  }
  all_invariants_hold: boolean
  duration_ms: number
  errors: string[]
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface OsTuneCheck {
  id: string
  label: string
  status: string
  detail: string
  [key: string]: unknown
}

export interface OsTuneReceipt {
  status: 'PASSED' | 'FAILED'
  page: string
  kind: string
  platform: string
  platform_release: string
  profile: string
  ring: string
  apply_requested: boolean
  tuned: boolean
  checks: OsTuneCheck[]
  groups: Record<string, OsTuneCheck[]>
  applied: string[]
  recommended: string[]
  applied_count: number
  recommended_count: number
  unavailable: string[]
  error_count: number
  sockets: Record<string, unknown>
  socket_telemetry: {
    transport: string
    tcp_nodelay: number
    so_keepalive: number
    recv_buffer_bytes: number
    send_buffer_bytes: number
    errors: string[]
  }
  transports: Record<string, unknown>
  all_invariants_hold: boolean
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface MiroFishTickBreakdown {
  tick: number
  net_flow: number
  archetypes: Array<{ name: string; weight: number; flow: number }>
}

export interface MiroFishShockReceipt {
  status: string
  page: string
  kind: string
  symbol: string
  silo: string
  sentiment_tag: string
  shock: Record<string, unknown>
  archetype_count: number
  archetypes: string[]
  tick_count: number
  tick_flows: number[]
  tick_breakdown: MiroFishTickBreakdown[]
  archetype_weights: number[]
  cascade_fragility_index: number
  crowd_asymmetry_score: number
  narrative_exhaustion_divergence: number
  fragility_halt_threshold: number
  requires_ring1_halt: boolean
  fragility_override: boolean
  revoked_long_leases: number
  latency_ms: number
  engine_latency_ms: number
  sla_ms: number
  within_sla: boolean
  sha256_digest: string
  grounding: Record<string, unknown>
  packet: Record<string, unknown>
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface CommitteeTranscriptStage {
  stage: string
  actor: string
  text: string
  verified: boolean
  claims_checked: number
}

export interface CommitteeDebateReceipt {
  status: string
  page: string
  kind: string
  silo: string
  requested_symbol: string
  symbol: string
  symbol_match: boolean
  rank: number
  score: number
  price: number
  universe_size: number
  verdict: 'CONVICTED' | 'ABSTAINED'
  abstention_reason: string
  transcript: CommitteeTranscriptStage[]
  stage_count: number
  report: Record<string, unknown>
  proposal: Record<string, unknown>
  sha256_digest: string
  grounding: Record<string, unknown>
  packet: Record<string, unknown>
  contagion: Record<string, unknown>
  elapsed_ms: number
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface ChallengerEvalReceipt {
  status: 'PROMOTED' | 'REJECTED'
  page: string
  kind: string
  plugin_name: string
  genome_id: string
  silo: string
  candidate_sharpe: number
  shadow_fills: number
  winning_shadow_fills: number
  recorded_fills: number
  gates: Record<
    string,
    { measured: number | boolean; required: number | boolean; passed: boolean }
  >
  gate_results: {
    dsr_floor_met: boolean
    shadow_nursery_met: boolean
    nursery_graduated: boolean
  }
  promotion_ready: boolean
  rejection_reasons: string[]
  hold_reason: string
  nursery: Record<string, unknown>
  genome: Record<string, unknown>
  thresholds: Record<string, unknown>
  forge: Record<string, unknown>
  activation: string
  note: string
  operator: string
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// V1.2 - agent task assignment workstation (/agents)
// ---------------------------------------------------------------------------

export interface StrategyTemplate {
  strategy_id: string
  name: string
  family: string
  summary: string
  timeframe: string
  required_bars: number
  donor_source: string
  donor_file: string
  silos: string[]
  parameters: Record<string, number>
  [key: string]: unknown
}

export interface AgentSpec {
  agent_id: string
  title: string
  task_type: string
  description: string
  capabilities: string[]
  requires_strategy: boolean
  token_prompt: number
  token_completion: number
  token_cost_usd: number
  ring: string
  signs_orders: boolean
  reads_broker_keys: boolean
  [key: string]: unknown
}

export interface TokenUsage {
  spend_usd: number
  cap_usd: number
  remaining_usd: number
  utilization_pct: number
  calls: number
  prompt_tokens: number
  completion_tokens: number
  locked: boolean
  [key: string]: unknown
}

export interface AgentTaskRow {
  task_id: string
  agent_id: string
  task_type: string
  silo: string
  symbols: string[]
  strategy_id: string | null
  prompt: string
  priority: string
  status: string
  progress_pct: number
  created_at_ns: number
  completed_at_ns: number
  latency_ms: number
  token_cost_usd: number
  grounding_sha256: string
  verified: boolean
  error: string | null
  [key: string]: unknown
}

export interface AgentAssignPayload {
  agent_id: string
  silo: string
  symbols: string[]
  strategy_id: string | null
  prompt: string
  priority: string
}

export interface AgentAssignReceipt extends AgentTaskRow {
  structured_result: Record<string, unknown>
  markdown_report: string
  verification: Record<string, unknown>
  token?: TokenUsage
}

export interface AgentCancelReceipt {
  cancelled: number
  scope: string
  remaining_tasks: number
  tasks: AgentTaskRow[]
  token?: TokenUsage
  [key: string]: unknown
}

export interface AgentWatchlistRankRow {
  symbol: string
  rank: number
  signal_score: number
  operation_advice: string
  close_price: number
  target_price: number
  stop_loss: number
  reward_risk: number
  rsi_14: number
  risk_factors: string[]
  best_strategy_id: string
  grounding_sha256: string
  verified: boolean
  [key: string]: unknown
}

export interface AgentBatchWatchlistPayload {
  silo: string
  symbols: string[]
  strategy_id?: string | null
}

export interface AgentBatchWatchlistReceipt extends AgentAssignReceipt {
  structured_result: {
    batch?: boolean
    watchlist_size?: number
    ranking?: AgentWatchlistRankRow[]
    top_symbol?: string | null
    advice_by_symbol?: Record<string, string>
    score_by_symbol?: Record<string, number>
    [key: string]: unknown
  }
}

export interface AgentConversationTurn {
  role: string
  text: string
  at_ns: number
  grounding_sha256?: string
  verified?: boolean
  claims_checked?: number
  token_cost_usd?: number
  [key: string]: unknown
}

export interface AgentFollowupPayload {
  task_id: string
  question: string
}

export interface AgentFollowupReceipt {
  task_id: string
  agent_id: string
  silo: string
  symbols: string[]
  question: string
  answer: string
  intents: string[]
  conversation_history: AgentConversationTurn[]
  turn_index: number
  grounding_sha256: string
  verified: boolean
  verification: Record<string, unknown>
  generated_at_ns: number
  [key: string]: unknown
}

export interface AgentsPageData {
  page: string
  route: string
  workspaces: string[]
  routes: Record<string, string>
  agent_count: number
  agents: AgentSpec[]
  strategy_count: number
  strategies: StrategyTemplate[]
  silos: string[]
  priorities: string[]
  task_count: number
  tasks: AgentTaskRow[]
  counts: Record<string, number>
  active_tasks: number
  completed_tasks: number
  failed_tasks: number
  cancelled_tasks: number
  token: TokenUsage
  clean_slate: boolean
  air_gap: Record<string, unknown>
  generated_at_ns: number
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// V1.2 - walk-forward backtesting workstation (/backtest)
// ---------------------------------------------------------------------------

/** Selectable bar frequencies for the walk-forward backtester. */
export type BacktestTimeframe = '1m' | '5m' | '15m' | '1h' | '1d'

export interface CostFloorRow {
  silo: string
  cost_floor_bps: number
  statutory_tax_bps: number
  [key: string]: unknown
}

export interface BacktestSummaryRow {
  backtest_id: string
  silo: string
  symbol: string
  strategy_id: string
  strategy_name: string
  bars: number
  cost_floor_bps: number
  net_edge_bps: number
  deflated_sharpe_ratio: number
  passes_dsr_gate: boolean
  passes_champion_floor: boolean
  promotable: boolean
  grounding_sha256: string
  verified: boolean
  created_at_ns: number
  initial_capital_units: number
  final_nav_units: number
  total_return_pct: number
  max_drawdown_pct: number
  total_trades: number
  win_rate_pct: number
  params: Record<string, unknown>
  [key: string]: unknown
}

export interface BacktestPageData {
  page: string
  route: string
  workspaces: string[]
  routes: Record<string, string>
  run_count: number
  runs: BacktestSummaryRow[]
  strategy_count: number
  strategies: StrategyTemplate[]
  silos: string[]
  timeframes: BacktestTimeframe[]
  default_timeframe: BacktestTimeframe
  cost_floors: Record<string, number>
  cost_floor_table: CostFloorRow[]
  k_folds: number
  embargo_pct: number
  dsr_gate: number
  champion_net_edge_floor_bps: number
  report_tolerance: number
  clean_slate: boolean
  air_gap: Record<string, unknown>
  generated_at_ns: number
  [key: string]: unknown
}

export interface BacktestTradeRow {
  trade_id: string
  entry_bar: number
  exit_bar: number
  side: string
  entry_price: number
  exit_price: number
  gross_bps: number
  cost_bps: number
  net_bps: number
  bars_held: number
  reason: string
  [key: string]: unknown
}

export interface HorizonRow {
  horizon_bars: number
  samples: number
  wins: number
  win_rate_pct: number
  net_edge_bps: number
  cost_bps: number
  [key: string]: unknown
}

export interface PurgedKFoldReport {
  k: number
  embargo_pct: number
  embargo_bars: number
  fold_count: number
  folds: Array<Record<string, unknown>>
  trades_in_folds: number
  out_of_sample_net_bps: number
  purged: number
  embargoed: number
  leakage_free: boolean
  [key: string]: unknown
}

export interface BacktestRunPayload {
  silo: string
  symbol: string
  strategy_id: string
  bars: number
  timeframe: BacktestTimeframe
  initial_capital_units: number
  fast_window?: number | null
  slow_window?: number | null
  stop_loss_bps?: number | null
  take_profit_bps?: number | null
}

export interface BacktestReceipt {
  backtest_id: string
  silo: string
  symbol: string
  strategy_id: string
  strategy_name: string
  bars: number
  timeframe: BacktestTimeframe
  bars_per_year: number
  bar_interval_ns: number
  cost_floor_bps: number
  statutory_tax_bps: number
  initial_capital_units: number
  final_nav_units: number
  benchmark_final_units: number
  equity_curve: number[]
  drawdown_curve_pct: number[]
  benchmark_curve_units: number[]
  trades: BacktestTradeRow[]
  trade_count: number
  signal_bars: number
  metrics: Record<string, number>
  horizon_matrix: Record<string, HorizonRow>
  purged_kfold: PurgedKFoldReport
  deflated_sharpe_ratio: number
  dsr_gate: number
  passes_dsr_gate: boolean
  net_edge_bps: number
  champion_net_edge_floor_bps: number
  passes_champion_floor: boolean
  promotable: boolean
  grounding_sha256: string
  fact_count: number
  markdown_report: string
  verification: Record<string, unknown>
  verified: boolean
  created_at_ns: number
  run_count?: number
  [key: string]: unknown
}

export interface NurseryPromotionReceipt {
  promoted: boolean
  already_registered: boolean
  backtest_id: string
  strategy_id: string
  silo: string
  symbol: string
  deflated_sharpe_ratio: number
  dsr_gate: number
  net_edge_bps: number
  champion_net_edge_floor_bps: number
  plugin_name: string
  shadow_fills: number
  required_fills: number
  remaining_fills: number
  graduated: boolean
  nursery_bypassed: boolean
  grounding_sha256: string
  next: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface BacktestComparePayload {
  silo: string
  symbol: string
  timeframe: BacktestTimeframe
  bars: number
}

export interface BacktestComparisonRow {
  rank: number
  strategy_id: string
  strategy_name: string
  backtest_id: string
  total_return_pct: number
  sharpe_ratio: number
  deflated_sharpe_ratio: number
  passes_dsr_gate: boolean
  net_edge_bps: number
  win_rate_pct: number
  total_trades: number
  max_drawdown_pct: number
  cost_floor_bps: number
  grounding_sha256: string
  verified: boolean
  [key: string]: unknown
}

export interface BacktestComparisonReceipt {
  silo: string
  symbol: string
  timeframe: BacktestTimeframe
  bars: number
  bars_per_year: number
  bar_interval_ns: number
  cost_floor_bps: number
  statutory_tax_bps: number
  dsr_gate: number
  strategy_count: number
  leaderboard: BacktestComparisonRow[]
  best_strategy: BacktestComparisonRow | null
  comparison_sha256: string
  generated_at_ns: number
  [key: string]: unknown
}


// ---------------------------------------------------------------------------
// V1.3 - options chain, market terminal, trade logbook and 12-venue broker hub
// ---------------------------------------------------------------------------

export interface GroundedPacketEnvelope {
  sha256_digest: string
  verified: boolean
  claims_checked: number
  derived_expressions: number
  tolerance: number
  packet: Record<string, unknown>
  report: Record<string, unknown>
  ledger: { packets: number; digest: string } | null
}

export interface OptionsUnderlying {
  symbol: string
  silo: string
  currency: string
  spot: number
  volatility: number
  days_to_expiry: number
  strike_step: number
  lot_size: number
}

export interface OptionsStrategyTemplate {
  id: string
  name: string
  bias: string
  legs: string
  summary: string
}

export interface OptionsChainRow {
  strike: number
  moneyness: number
  offset: number
  call_ltp: number
  call_iv: number
  call_delta: number
  call_gamma: number
  call_theta: number
  call_vega: number
  call_oi: number
  put_ltp: number
  put_iv: number
  put_delta: number
  put_gamma: number
  put_theta: number
  put_vega: number
  put_oi: number
  [key: string]: unknown
}

export interface OptionsChainSummary {
  atm_strike: number
  atm_iv: number
  pcr: number
  pcr_regime: string
  max_pain_strike: number
  max_pain_value: number
  total_call_oi: number
  total_put_oi: number
  strike_count: number
}

export interface OptionsChainReceipt {
  status: string
  page: string
  symbol: string
  silo: string
  currency: string
  lot_size: number
  strike_step: number
  underlying: {
    spot: number
    volatility: number
    days_to_expiry: number
    years_to_expiry: number
    risk_free_rate: number
  }
  chain: OptionsChainRow[]
  summary: OptionsChainSummary
  grounding: GroundedPacketEnvelope
  packet: Record<string, unknown>
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface OptionsPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  underlyings: OptionsUnderlying[]
  strategies: OptionsStrategyTemplate[]
  default_symbol: string
  chain: OptionsChainReceipt
  counts: { underlyings: number; strategies: number; strikes: number }
  law: string
  generated_at_ns: number
}

export interface OptionLeg {
  leg: string
  kind: string
  direction: number
  strike: number
  strike_offset_steps: number
  iv: number
  premium: number
  delta: number
  gamma: number
  theta: number
  vega: number
  lot_size: number
}

export interface PayoffPoint {
  expiry_spot: number
  pnl: number
  return_bps: number
}

export interface StrategyPayoffReceipt {
  status: string
  page: string
  symbol: string
  silo: string
  currency: string
  strategy: string
  strategy_meta: Partial<OptionsStrategyTemplate>
  underlying: {
    spot: number
    volatility: number
    days_to_expiry: number
    years_to_expiry: number
    risk_free_rate: number
  }
  atm_strike: number
  lot_size: number
  legs: OptionLeg[]
  net_premium_per_unit: number
  net_debit_credit: number
  net_position: string
  max_profit: number
  max_loss: number
  unbounded_upside: boolean
  unbounded_downside: boolean
  breakeven_points: number[]
  payoff_curve: PayoffPoint[]
  curve_points: number
  grounding: GroundedPacketEnvelope
  packet: Record<string, unknown>
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface TerminalCandle {
  index: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface TerminalQuoteRow {
  symbol: string
  silo: string
  asset_class: string
  last: number
  change_bps: number
  bid: number
  ask: number
  spread_bps: number
}

export interface TerminalDepthLevel {
  level: number
  price: number
  quantity: number
}

export interface TerminalTapePrint {
  sequence: number
  price: number
  quantity: number
  side: string
  classification: string
}

export interface TerminalFrame {
  status: string
  page: string
  symbol: string
  silo: string
  asset_class: string
  quote: {
    last: number
    mid: number
    reference: number
    session_change_bps: number
    spread_bps: number
  }
  quotes: TerminalQuoteRow[]
  candles: TerminalCandle[]
  series: {
    close: number[]
    ma5: number[]
    ma10: number[]
    ma20: number[]
    vwap: number[]
    volume: number[]
  }
  depth: {
    level_count: number
    bids: TerminalDepthLevel[]
    asks: TerminalDepthLevel[]
    bid_quantity_total: number
    ask_quantity_total: number
    imbalance_ratio: number
    imbalance_regime: string
  }
  tape: TerminalTapePrint[]
  tape_summary: {
    prints: number
    buy_initiated: number
    sell_initiated: number
    buy_ratio: number
  }
  grounding: GroundedPacketEnvelope
  packet: Record<string, unknown>
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface TerminalWatchRow {
  symbol: string
  silo: string
  asset_class: string
  reference: number
}

export interface TerminalPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  watchlist: TerminalWatchRow[]
  default_symbol: string
  frame: TerminalFrame
  counts: { watchlist: number; silos: number; bars: number; depth_levels: number }
  law: string
  generated_at_ns: number
}

export interface LogbookEntryRecord {
  entry_id: string
  sequence: number
  silo: string
  symbol: string
  side: string
  setup_strategy: string
  entry_price: number
  exit_price: number
  quantity: number
  stop_loss: number
  notional: number
  gross_pnl: number
  cost_bps: number
  cost_drag: number
  net_pnl: number
  net_bps: number
  r_multiple: number
  outcome: string
  workspace: string
  thesis: string
  lessons_learned: string
  recorded_at_ns: number
  [key: string]: unknown
}

export interface LogbookPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  entries: LogbookEntryRecord[]
  counts: {
    entries: number
    wins: number
    losses: number
    flat: number
    paper: number
    live: number
  }
  aggregate: {
    gross_pnl: number
    cost_drag: number
    net_pnl: number
    win_rate: number
    avg_r_multiple: number
    total_r_multiple: number
    avg_cost_bps: number
  }
  cost_floor_bps: Record<string, number>
  clean_slate: boolean
  law: string
  generated_at_ns: number
}

export interface LogbookEntryPayload {
  silo: string
  symbol: string
  side: 'LONG' | 'SHORT' | 'BUY' | 'SELL'
  setup_strategy?: string
  entry_price: number
  exit_price: number
  quantity: number
  stop_loss?: number
  thesis?: string
  lessons_learned?: string
  workspace?: 'PAPER' | 'LIVE'
}

export interface LogbookEntryReceipt {
  ok: boolean
  page: string
  workspace: string
  entry: LogbookEntryRecord
  counts: LogbookPageData['counts']
  aggregate: LogbookPageData['aggregate']
  law: string
  generated_at_ns: number
  [key: string]: unknown
}

export interface BrokerVenueRow {
  venue: string
  silo: string
  market: string
  adapter_module: string
  adapter_importable: boolean
  adapter_reason: string
  credential_status: string
  api_key_masked: string | null
  api_secret_masked: string | null
  environment: string | null
  dry_run_verified: boolean
  signing: { algorithm: string; dry_run_verified: boolean; public_fingerprint: string }
  feed: { mode: string; dual_vendor: boolean; health: string }
  settlement: Record<string, unknown>
  [key: string]: unknown
}

export interface BrokersPageData {
  page: string
  title: string
  ring: string
  read_only: boolean
  venues_supported: string[]
  venues: BrokerVenueRow[]
  silos: Array<{ silo: string; market: string; venues: string[]; configured: number }>
  vault: {
    write_only: boolean
    path: string
    key_version: number | null
    ed25519_public_fingerprint: string | null
    raw_secrets_exposed: boolean
  }
  signing_dry_run: Record<string, unknown>
  feed: Record<string, unknown>
  counts: {
    venues: number
    silos: number
    configured: number
    unset: number
    adapters_importable: number
  }
  air_gap: { raw_keys_exposed: number; order_signing_tools: number; ring3_read_only: boolean }
  law: string
  generated_at_ns: number
}

export interface BrokerPingReceipt {
  ok: boolean
  page: string
  venue: string
  silo: string
  adapter: { module: string; importable: boolean; reason: string }
  signing: Record<string, unknown>
  credential: { status: string; api_key_masked: string | null; raw_secret_returned: boolean }
  latency_budget_ms: number
  operator: string
  generated_at_ns: number
  [key: string]: unknown
}
