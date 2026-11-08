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

export interface CockpitState {
  version: string
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
