/**
 * V1.4 wire contracts.
 *
 * The gateway is the source of truth; these interfaces mirror the payloads the
 * cockpit actually reads. Telemetry records that are still evolving carry an
 * index signature so a new backend field can never break a render, while the
 * fields the UI depends on stay explicitly typed.
 */

export type Tier = "NONE" | "PAPER" | "LIVE";
export type WorkspaceMode = "paper" | "live";
export type Workspace = "base" | WorkspaceMode;

export type JsonRecord = Record<string, unknown>;

/* ------------------------------- auth ------------------------------- */

export interface AuthStatus {
  initialized: boolean;
  authenticated: boolean;
  username: string | null;
  tier: Tier;
  workspace?: Tier;
  clean_slate_active: boolean;
  login_portals?: { paper: string; live: string; gateway: string };
  active_workspace?: Tier;
  issued_ns?: number;
  expires_ns?: number;
}

export interface AuthSession {
  ok: boolean;
  token: string;
  username: string;
  tier: Tier;
  workspace?: Tier;
  portal?: string;
  issued_ns: number;
  expires_ns: number;
  session_ttl_s?: number;
  clean_slate?: boolean;
  preflight_ok?: boolean;
  warnings?: string[];
}

export interface VaultBalance {
  micros: number;
  nav: number;
}

export interface PaperSiloState {
  vaults: JsonRecord;
  nav_micros: number;
  nav?: number;
  mode?: string;
}

export interface PaperWorkspaceState {
  workspace: "paper";
  mode: "PAPER";
  tier: Tier;
  username: string | null;
  clean_slate_active: boolean;
  silos: Record<string, PaperSiloState>;
  orders: { working: JsonRecord[]; events: JsonRecord[]; counts: Record<string, number> };
  ledger: {
    vaults: Record<string, JsonRecord>;
    double_entry: Record<string, number>;
    counts: Record<string, number>;
  };
  shadow: {
    shadow_fills: number;
    required_fills: number;
    remaining_fills: number;
    graduated: boolean;
    live_fills: number;
  };
  autopilot: { enabled: boolean; paused?: boolean; ticks?: number };
  token_spend_usd: number;
  generated_at_ns: number;
}

export interface LiveWorkspaceState {
  workspace: "live";
  mode: "LIVE";
  tier: Tier;
  username: string | null;
  silos: Record<string, { mode?: string; broker?: string; configured?: boolean; frozen?: boolean }>;
  broker_credentials: JsonRecord;
  preflight: JsonRecord;
  feed: JsonRecord;
  testnet: JsonRecord;
  kill_switch: JsonRecord;
  live_fills: number;
  generated_at_ns: number;
}

/* ------------------------------ cockpit ------------------------------ */

export interface SiloState {
  label?: string;
  mode?: string;
  status?: string;
  frozen?: boolean;
  halted?: boolean;
  vaults?: JsonRecord;
  nav_micros?: number;
  nav?: number;
  cost_floor_bps?: number;
  statutory_tax_bps?: number;
  broker?: string;
  [key: string]: unknown;
}

export interface LeaseRow {
  silo?: string;
  strategy_id?: string;
  state?: string;
  ttl_s?: number;
  remaining_s?: number;
  expires_ns?: number;
  [key: string]: unknown;
}

export interface CockpitCard {
  card_id?: string;
  id?: string;
  title?: string;
  headline?: string;
  body?: string;
  detail?: string;
  tone?: string;
  ring?: string;
  proof_sha256?: string;
  grounding_sha256?: string;
  created_at_ns?: number;
  [key: string]: unknown;
}

export interface SystemState {
  status?: string;
  mode?: string;
  version?: string;
  uptime_s?: number;
  [key: string]: unknown;
}

export interface RingProcess {
  index: number;
  worker: string;
  pid: number | null;
  alive: boolean;
  spawned: boolean;
  heartbeat_age_ms: number | null;
  heartbeat_pid?: number | null;
  last_beat_ns?: number | null;
  restarts: number;
  cpu_core: number | null;
  exit_code: number | null;
}

export interface RingProcessesState {
  available: boolean;
  reason?: string;
  rings: RingProcess[];
  pids: Record<string, number | null>;
  distinct_os_processes: number;
  running?: boolean;
}

export interface PreflightGate {
  gate: string;
  label: string;
  status: string;
  detail: string;
  evidence?: JsonRecord;
  duration_ms?: number;
}

export interface PreflightCertificate {
  kind: string;
  release: string;
  ready_for_live_cutover: boolean;
  passed_gates: number;
  failed_gates: number;
  warned_gates: number;
  total_gates: number;
  failed_gate_ids: string[];
  warned_gate_ids: string[];
  certificate_sha256: string;
  gates: PreflightGate[];
  served_from?: string;
  generated_at_ns?: number | null;
  next_action?: string;
}

export interface FormalProofSummary {
  states_explored: number;
  counterexamples: number;
  tla_ok: boolean;
  vopr_ok: boolean;
  tigerbeetle_ready: boolean;
  postgres_ready: boolean;
  tail_hedger_ready: boolean;
}

export interface FormalProofState {
  kind: string;
  tla: JsonRecord | null;
  vopr: JsonRecord | null;
  tigerbeetle: JsonRecord | null;
  postgres: JsonRecord | null;
  tail_hedger: JsonRecord | null;
  summary: FormalProofSummary;
  ok: boolean;
  checked_at_ns: number;
}

export interface CockpitState {
  version: string;
  document_id: string;
  system: SystemState;
  abstention: JsonRecord;
  silos: Record<string, SiloState>;
  brokers: { order: string[]; silos: Record<string, JsonRecord> };
  leases: LeaseRow[];
  contagion: JsonRecord;
  llm: JsonRecord;
  cards: CockpitCard[];
  kill_switch: JsonRecord | null;
  extensions: JsonRecord;
  formal_proofs: FormalProofState;
  autopilot: { enabled?: boolean; paused?: boolean; ticks?: number; [key: string]: unknown };
  ring_processes?: RingProcessesState;
  feed_mode?: string;
  feed?: JsonRecord;
  broker_credentials?: JsonRecord;
  llm_probe?: JsonRecord;
  streams: { cockpit: string; fps: number };
}

export interface PageSpec {
  key: string;
  title: string;
  route: string;
  aliases: string[];
  ring: string;
  read_only: boolean;
  summary: string;
  live?: boolean;
  status?: string;
  api?: string;
}

export interface PagesIndex {
  version: string;
  page_count: number;
  pages: PageSpec[];
  routes: Record<string, string>;
  counts: Record<string, number | boolean>;
  air_gap: JsonRecord;
  generated_at_ns: number;
}

/* ------------------------------- orders ------------------------------- */

export interface OrdersPageData {
  page: string;
  oms: JsonRecord;
  working_orders: JsonRecord[];
  twap_icebergs: JsonRecord[];
  events: JsonRecord[];
  outbox: JsonRecord;
  reservations: JsonRecord[];
  pending_reservations: number;
  positions: JsonRecord;
  reconciliation: JsonRecord;
  counts: Record<string, number>;
  generated_at_ns: number;
}

export interface OrderSide {
  side: "BUY" | "SELL";
}

export interface PaperOrderPayload {
  silo: string;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price?: number;
  order_style?: "MAKER_PEG" | "TWAP_ICEBERG";
  [key: string]: unknown;
}

export interface RatchetSplit {
  long_term_pct?: number;
  reserve_pct?: number;
  silo_pct?: number;
  [key: string]: unknown;
}

export interface PaperOrderReceipt {
  status?: string;
  order_id?: string;
  silo?: string;
  symbol?: string;
  side?: string;
  filled_quantity?: number;
  fill_price?: number;
  net_bps?: number;
  cost_bps?: number;
  ratchet?: RatchetSplit;
  outbox?: JsonRecord;
  sha256_digest?: string;
  [key: string]: unknown;
}

export interface CancelOrdersReceipt {
  cancelled?: number;
  order_id?: string;
  pending_voided?: number;
  [key: string]: unknown;
}

/* ------------------------------ analyzer ------------------------------ */

export interface AnalyzerStrategyRow {
  strategy_id: string;
  graduated: boolean;
  required_fills: number;
  completed_fills: number;
  remaining_fills: number;
  progress_pct: number;
  q98_bps: number;
  adverse_selection_bps: number;
  report: JsonRecord;
}

export interface AnalyzerPageData {
  page: string;
  nursery: {
    required_fills: number;
    calibrated: boolean;
    graduated_count: number;
    graduated_strategies: string[];
    strategies: AnalyzerStrategyRow[];
  };
  paper: Record<string, number>;
  live: Record<string, number>;
  source: { package: string; components: string[] };
  generated_at_ns: number;
}

export interface ShadowBurstReceipt {
  status?: string;
  silo?: string;
  requested?: number;
  fills?: number;
  shadow_fills?: number;
  graduated?: boolean;
  ticks?: JsonRecord[];
  [key: string]: unknown;
}

/* -------------------------------- quant -------------------------------- */

export interface QuantScanCandidate {
  rank: number;
  symbol: string;
  score: number;
  price: number;
  feature_count?: number;
  sha256_digest?: string;
}

export interface QuantScan {
  available?: boolean;
  error?: string;
  candidates?: QuantScanCandidate[];
  elapsed_ms?: number;
  universe_size?: number;
  factor_count?: number;
  as_of_ns?: number;
  scan_id?: string;
  kernel_count?: number;
  donor_factor_count?: number;
}

export interface ActivationEntryInfo {
  module: string;
  ok: boolean;
  error: string;
  exports: string[];
}

export interface ActivationMatrix {
  ok: boolean;
  import_errors: number;
  loader_file_count: number;
  engine_file_count: number;
  loader_module_count: number;
  engine_module_count: number;
  total_modules: number;
  registry_sources: string[];
  engine_classes: string[];
  loaders: ActivationEntryInfo[];
  engines: ActivationEntryInfo[];
  failures: string[];
}

export interface QuantPageData {
  page: string;
  universe_scan: QuantScan;
  factors: { count: number; kernels: number; warm: boolean };
  valuation: JsonRecord;
  tail_hedger: JsonRecord;
  datasets: ActivationMatrix;
  generated_at_ns: number;
}

export interface OptionPriceReceipt {
  status?: string;
  price?: number;
  greeks?: {
    delta?: number;
    gamma?: number;
    vega?: number;
    theta?: number;
    rho?: number;
    [key: string]: unknown;
  };
  inputs?: JsonRecord;
  sha256_digest?: string;
  grounding?: JsonRecord;
  verified?: boolean;
  [key: string]: unknown;
}

export interface InteractiveQuantScan {
  status?: string;
  available?: boolean;
  candidates?: QuantScanCandidate[];
  elapsed_ms?: number;
  universe_size?: number;
  factor_count?: number;
  sha256_digest?: string;
  [key: string]: unknown;
}

/* -------------------------------- forge -------------------------------- */

export interface ForgeLeaderRow {
  strategy_id: string;
  role: string;
  deflated_sharpe: number;
  net_edge_bps: number;
  promotion_ready: boolean;
  above_floor: boolean;
  [key: string]: unknown;
}

export interface ForgePageData {
  page: string;
  forge: JsonRecord;
  thresholds: Record<string, number>;
  leaderboard: ForgeLeaderRow[];
  next_run: { seconds_until_next_run: number; next_run_ns: number; schedule_utc: string };
  plugins: JsonRecord;
  skills: JsonRecord;
  generated_at_ns: number;
}

export interface SkillMarkdownReceipt {
  name?: string;
  path?: string;
  markdown?: string;
  bytes?: number;
  sha256_digest?: string;
  [key: string]: unknown;
}

export interface ChallengerEvalReceipt {
  status: "PROMOTED" | "REJECTED";
  plugin_name: string;
  candidate_sharpe: number;
  shadow_fills: number;
  gates: Record<string, { measured: number | boolean; required: number | boolean; passed: boolean }>;
  promotion_ready: boolean;
  rejection_reasons: string[];
  note: string;
  [key: string]: unknown;
}

/* -------------------------------- ledger -------------------------------- */

export interface LedgerPageData {
  page: string;
  silos: Record<string, Record<string, unknown>>;
  transfers: JsonRecord[];
  double_entry: { debits_micros: number; credits_micros: number; balanced: boolean };
  ratchet: { long_term_pct: number; reserve_pct: number; cross_border_wire_bps: number };
  invariant: JsonRecord;
  backends: JsonRecord;
  counts: Record<string, number>;
  generated_at_ns: number;
}

export interface LedgerReconcileReceipt {
  status?: string;
  silos?: JsonRecord[];
  balanced?: boolean;
  drift_bps?: number;
  [key: string]: unknown;
}

/* -------------------------------- logbook -------------------------------- */

export interface LogbookEntryRecord {
  entry_id: string;
  sequence: number;
  silo: string;
  symbol: string;
  side: string;
  setup_strategy: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  stop_loss: number;
  notional: number;
  gross_pnl: number;
  cost_bps: number;
  cost_drag: number;
  net_pnl: number;
  net_bps: number;
  r_multiple: number;
  outcome: string;
  workspace: string;
  thesis: string;
  lessons_learned: string;
  recorded_at_ns: number;
  [key: string]: unknown;
}

export interface LogbookPageData {
  page: string;
  entries: LogbookEntryRecord[];
  counts: {
    entries: number;
    wins: number;
    losses: number;
    flat: number;
    paper: number;
    live: number;
  };
  aggregate: {
    gross_pnl: number;
    cost_drag: number;
    net_pnl: number;
    win_rate: number;
    avg_r_multiple: number;
    total_r_multiple: number;
    avg_cost_bps: number;
  };
  cost_floor_bps: Record<string, number>;
  clean_slate: boolean;
  generated_at_ns: number;
}

export interface LogbookEntryPayload {
  silo: string;
  symbol: string;
  side: "LONG" | "SHORT" | "BUY" | "SELL";
  setup_strategy?: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  stop_loss?: number;
  thesis?: string;
  lessons_learned?: string;
  workspace?: "PAPER" | "LIVE";
}

/* -------------------------------- brokers -------------------------------- */

export interface BrokerVenueRow {
  venue: string;
  silo: string;
  market: string;
  adapter_module: string;
  adapter_importable: boolean;
  adapter_reason: string;
  credential_status: string;
  api_key_masked: string | null;
  api_secret_masked: string | null;
  environment: string | null;
  dry_run_verified: boolean;
  signing: { algorithm: string; dry_run_verified: boolean; public_fingerprint: string };
  feed: { mode: string; dual_vendor: boolean; health: string };
  settlement: JsonRecord;
  [key: string]: unknown;
}

export interface BrokersPageData {
  page: string;
  venues_supported: string[];
  venues: BrokerVenueRow[];
  silos: Array<{ silo: string; market: string; venues: string[]; configured: number }>;
  vault: {
    write_only: boolean;
    path: string;
    key_version: number | null;
    ed25519_public_fingerprint: string | null;
    raw_secrets_exposed: boolean;
  };
  signing_dry_run: JsonRecord;
  feed: JsonRecord;
  counts: {
    venues: number;
    silos: number;
    configured: number;
    unset: number;
    adapters_importable: number;
  };
  air_gap: { raw_keys_exposed: number; order_signing_tools: number; ring3_read_only: boolean };
  generated_at_ns: number;
}

export interface BrokerPingReceipt {
  ok: boolean;
  venue: string;
  silo: string;
  adapter: { module: string; importable: boolean; reason: string };
  signing: JsonRecord;
  credential: { status: string; api_key_masked: string | null; raw_secret_returned: boolean };
  latency_budget_ms: number;
  [key: string]: unknown;
}

/* --------------------------------- mcp --------------------------------- */

export interface McpPageData {
  page: string;
  tools: Array<{ name?: string; description?: string; read_only?: boolean; [key: string]: unknown }>;
  count: number;
  air_gap: JsonRecord;
  invariants: Record<string, number>;
  transport: string;
  console_ready: boolean;
  generated_at_ns: number;
}

export interface McpCallResult {
  name: string;
  ok: boolean;
  result?: unknown;
  error?: string;
  elapsed_ms: number;
}

/* -------------------------------- options -------------------------------- */

export interface OptionsUnderlying {
  symbol: string;
  silo: string;
  currency: string;
  spot: number;
  volatility: number;
  days_to_expiry: number;
  strike_step: number;
  lot_size: number;
}

export interface OptionsStrategyTemplate {
  id: string;
  name: string;
  bias: string;
  legs: string;
  summary: string;
}

export interface OptionsChainRow {
  strike: number;
  moneyness: number;
  offset: number;
  call_ltp: number;
  call_iv: number;
  call_delta: number;
  call_gamma: number;
  call_theta: number;
  call_vega: number;
  call_oi: number;
  put_ltp: number;
  put_iv: number;
  put_delta: number;
  put_gamma: number;
  put_theta: number;
  put_vega: number;
  put_oi: number;
  [key: string]: unknown;
}

export interface GroundedPacketEnvelope {
  sha256_digest: string;
  verified: boolean;
  claims_checked: number;
  derived_expressions: number;
  tolerance: number;
  packet: JsonRecord;
  report: JsonRecord;
  ledger: { packets: number; digest: string } | null;
}

export interface OptionsChainReceipt {
  status: string;
  symbol: string;
  silo: string;
  currency: string;
  lot_size: number;
  strike_step: number;
  underlying: {
    spot: number;
    volatility: number;
    days_to_expiry: number;
    years_to_expiry: number;
    risk_free_rate: number;
  };
  chain: OptionsChainRow[];
  summary: {
    atm_strike: number;
    atm_iv: number;
    pcr: number;
    pcr_regime: string;
    max_pain_strike: number;
    max_pain_value: number;
    total_call_oi: number;
    total_put_oi: number;
    strike_count: number;
  };
  grounding: GroundedPacketEnvelope;
  generated_at_ns: number;
}

export interface OptionsPageData {
  page: string;
  underlyings: OptionsUnderlying[];
  strategies: OptionsStrategyTemplate[];
  default_symbol: string;
  chain: OptionsChainReceipt;
  counts: { underlyings: number; strategies: number; strikes: number };
  generated_at_ns: number;
}

export interface OptionLeg {
  leg: string;
  kind: string;
  direction: number;
  strike: number;
  strike_offset_steps: number;
  iv: number;
  premium: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  lot_size: number;
}

export interface PayoffPoint {
  expiry_spot: number;
  pnl: number;
  return_bps: number;
}

export interface StrategyPayoffReceipt {
  status: string;
  symbol: string;
  silo: string;
  currency: string;
  strategy: string;
  strategy_meta: Partial<OptionsStrategyTemplate>;
  underlying: {
    spot: number;
    volatility: number;
    days_to_expiry: number;
    years_to_expiry: number;
    risk_free_rate: number;
  };
  atm_strike: number;
  lot_size: number;
  legs: OptionLeg[];
  net_premium_per_unit: number;
  net_debit_credit: number;
  net_position: string;
  max_profit: number;
  max_loss: number;
  unbounded_upside: boolean;
  unbounded_downside: boolean;
  breakeven_points: number[];
  payoff_curve: PayoffPoint[];
  curve_points: number;
  grounding: GroundedPacketEnvelope;
  generated_at_ns: number;
}

/* -------------------------------- terminal -------------------------------- */

export interface TerminalCandle {
  index: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TerminalQuoteRow {
  symbol: string;
  silo: string;
  asset_class: string;
  last: number;
  change_bps: number;
  bid: number;
  ask: number;
  spread_bps: number;
}

export interface TerminalDepthLevel {
  level: number;
  price: number;
  quantity: number;
}

export interface TerminalTapePrint {
  sequence: number;
  price: number;
  quantity: number;
  side: string;
  classification: string;
}

export interface TerminalFrame {
  status: string;
  symbol: string;
  silo: string;
  asset_class: string;
  quote: {
    last: number;
    mid: number;
    reference: number;
    session_change_bps: number;
    spread_bps: number;
  };
  quotes: TerminalQuoteRow[];
  candles: TerminalCandle[];
  series: {
    close: number[];
    ma5: number[];
    ma10: number[];
    ma20: number[];
    vwap: number[];
    volume: number[];
  };
  depth: {
    level_count: number;
    bids: TerminalDepthLevel[];
    asks: TerminalDepthLevel[];
    bid_quantity_total: number;
    ask_quantity_total: number;
    imbalance_ratio: number;
    imbalance_regime: string;
  };
  tape: TerminalTapePrint[];
  tape_summary: {
    prints: number;
    buy_initiated: number;
    sell_initiated: number;
    buy_ratio: number;
  };
  grounding: GroundedPacketEnvelope;
  generated_at_ns: number;
}

export interface TerminalPageData {
  page: string;
  watchlist: Array<{ symbol: string; silo: string; asset_class: string; reference: number }>;
  default_symbol: string;
  frame: TerminalFrame;
  counts: { watchlist: number; silos: number; bars: number; depth_levels: number };
  generated_at_ns: number;
}

/* -------------------------------- agents -------------------------------- */

export interface StrategyTemplate {
  strategy_id: string;
  name: string;
  family: string;
  summary: string;
  timeframe: string;
  required_bars: number;
  donor_source: string;
  donor_file: string;
  silos: string[];
  parameters: Record<string, number>;
  [key: string]: unknown;
}

export interface AgentSpec {
  agent_id: string;
  title: string;
  task_type: string;
  description: string;
  capabilities: string[];
  requires_strategy: boolean;
  token_prompt: number;
  token_completion: number;
  token_cost_usd: number;
  ring: string;
  signs_orders: boolean;
  reads_broker_keys: boolean;
  [key: string]: unknown;
}

export interface TokenUsage {
  spend_usd: number;
  cap_usd: number;
  remaining_usd: number;
  utilization_pct: number;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  locked: boolean;
  [key: string]: unknown;
}

export interface AgentTaskRow {
  task_id: string;
  agent_id: string;
  task_type: string;
  silo: string;
  symbols: string[];
  strategy_id: string | null;
  prompt: string;
  priority: string;
  status: string;
  progress_pct: number;
  created_at_ns: number;
  completed_at_ns: number;
  latency_ms: number;
  token_cost_usd: number;
  grounding_sha256: string;
  verified: boolean;
  error: string | null;
  [key: string]: unknown;
}

export interface AgentAssignPayload {
  agent_id: string;
  silo: string;
  symbols: string[];
  strategy_id: string | null;
  prompt: string;
  priority: string;
}

export interface AgentAssignReceipt extends AgentTaskRow {
  structured_result: JsonRecord;
  markdown_report: string;
  verification: JsonRecord;
  token?: TokenUsage;
}

export interface AgentBatchWatchlistReceipt extends AgentAssignReceipt {
  structured_result: {
    batch?: boolean;
    watchlist_size?: number;
    ranking?: Array<{
      symbol: string;
      rank: number;
      signal_score: number;
      operation_advice: string;
      close_price: number;
      target_price: number;
      stop_loss: number;
      reward_risk: number;
      rsi_14: number;
      risk_factors: string[];
      best_strategy_id: string;
      grounding_sha256: string;
      verified: boolean;
      [key: string]: unknown;
    }>;
    top_symbol?: string | null;
    advice_by_symbol?: Record<string, string>;
    score_by_symbol?: Record<string, number>;
    [key: string]: unknown;
  };
}

export interface AgentFollowupReceipt {
  task_id: string;
  agent_id: string;
  question: string;
  answer: string;
  conversation_history: Array<{
    role: string;
    text: string;
    at_ns: number;
    verified?: boolean;
    claims_checked?: number;
    [key: string]: unknown;
  }>;
  turn_index: number;
  grounding_sha256: string;
  verified: boolean;
  [key: string]: unknown;
}

export interface AgentsPageData {
  page: string;
  agent_count: number;
  agents: AgentSpec[];
  strategy_count: number;
  strategies: StrategyTemplate[];
  silos: string[];
  priorities: string[];
  task_count: number;
  tasks: AgentTaskRow[];
  counts: Record<string, number>;
  active_tasks: number;
  completed_tasks: number;
  failed_tasks: number;
  cancelled_tasks: number;
  token: TokenUsage;
  clean_slate: boolean;
  generated_at_ns: number;
}

/* -------------------------------- backtest -------------------------------- */

export type BacktestTimeframe = "1m" | "5m" | "15m" | "1h" | "1d";

export interface CostFloorRow {
  silo: string;
  cost_floor_bps: number;
  statutory_tax_bps: number;
  [key: string]: unknown;
}

export interface BacktestSummaryRow {
  backtest_id: string;
  silo: string;
  symbol: string;
  strategy_id: string;
  strategy_name: string;
  bars: number;
  cost_floor_bps: number;
  net_edge_bps: number;
  deflated_sharpe_ratio: number;
  passes_dsr_gate: boolean;
  passes_champion_floor: boolean;
  promotable: boolean;
  grounding_sha256: string;
  verified: boolean;
  created_at_ns: number;
  initial_capital_units: number;
  final_nav_units: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  total_trades: number;
  win_rate_pct: number;
  params: JsonRecord;
  [key: string]: unknown;
}

export interface BacktestPageData {
  page: string;
  run_count: number;
  runs: BacktestSummaryRow[];
  strategy_count: number;
  strategies: StrategyTemplate[];
  silos: string[];
  timeframes: BacktestTimeframe[];
  default_timeframe: BacktestTimeframe;
  cost_floors: Record<string, number>;
  cost_floor_table: CostFloorRow[];
  k_folds: number;
  embargo_pct: number;
  dsr_gate: number;
  champion_net_edge_floor_bps: number;
  clean_slate: boolean;
  generated_at_ns: number;
}

export interface BacktestTradeRow {
  trade_id: string;
  entry_bar: number;
  exit_bar: number;
  side: string;
  entry_price: number;
  exit_price: number;
  gross_bps: number;
  cost_bps: number;
  net_bps: number;
  bars_held: number;
  reason: string;
  [key: string]: unknown;
}

export interface HorizonRow {
  horizon_bars: number;
  samples: number;
  wins: number;
  win_rate_pct: number;
  net_edge_bps: number;
  cost_bps: number;
  [key: string]: unknown;
}

export interface BacktestReceipt {
  backtest_id: string;
  silo: string;
  symbol: string;
  strategy_id: string;
  strategy_name: string;
  bars: number;
  timeframe: BacktestTimeframe;
  cost_floor_bps: number;
  statutory_tax_bps: number;
  initial_capital_units: number;
  final_nav_units: number;
  benchmark_final_units: number;
  equity_curve: number[];
  drawdown_curve_pct: number[];
  benchmark_curve_units: number[];
  trades: BacktestTradeRow[];
  trade_count: number;
  horizon_matrix: Record<string, HorizonRow>;
  purged_kfold: {
    k: number;
    embargo_pct: number;
    embargo_bars: number;
    fold_count: number;
    trades_in_folds: number;
    out_of_sample_net_bps: number;
    leakage_free: boolean;
  };
  deflated_sharpe_ratio: number;
  dsr_gate: number;
  passes_dsr_gate: boolean;
  net_edge_bps: number;
  champion_net_edge_floor_bps: number;
  passes_champion_floor: boolean;
  promotable: boolean;
  grounding_sha256: string;
  markdown_report: string;
  verified: boolean;
  created_at_ns: number;
  [key: string]: unknown;
}

export interface NurseryPromotionReceipt {
  promoted: boolean;
  backtest_id: string;
  strategy_id: string;
  silo: string;
  symbol: string;
  deflated_sharpe_ratio: number;
  shadow_fills: number;
  required_fills: number;
  remaining_fills: number;
  graduated: boolean;
  grounding_sha256: string;
  next: string;
  [key: string]: unknown;
}

export interface BacktestComparisonReceipt {
  silo: string;
  symbol: string;
  timeframe: BacktestTimeframe;
  bars: number;
  cost_floor_bps: number;
  statutory_tax_bps: number;
  dsr_gate: number;
  strategy_count: number;
  leaderboard: Array<{
    rank: number;
    strategy_id: string;
    strategy_name: string;
    backtest_id: string;
    total_return_pct: number;
    sharpe_ratio: number;
    deflated_sharpe_ratio: number;
    passes_dsr_gate: boolean;
    net_edge_bps: number;
    win_rate_pct: number;
    total_trades: number;
    max_drawdown_pct: number;
    cost_floor_bps: number;
    grounding_sha256: string;
    verified: boolean;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
}

/* -------------------------------- brain -------------------------------- */

export interface MiroFishShockReceipt {
  status: string;
  symbol: string;
  silo: string;
  sentiment_tag: string;
  archetype_count: number;
  archetypes: string[];
  tick_count: number;
  tick_flows: number[];
  tick_breakdown: Array<{
    tick?: number;
    archetype_flows?: number[];
    [key: string]: unknown;
  }>;
  archetype_weights: number[];
  cascade_fragility_index: number;
  crowd_asymmetry_score: number;
  narrative_exhaustion_divergence: number;
  fragility_halt_threshold: number;
  requires_ring1_halt: boolean;
  revoked_long_leases: number;
  latency_ms: number;
  sla_ms: number;
  within_sla: boolean;
  sha256_digest: string;
  [key: string]: unknown;
}

export interface CommitteeDebateReceipt {
  status: string;
  silo: string;
  requested_symbol: string;
  symbol: string;
  symbol_match: boolean;
  rank: number;
  score: number;
  price: number;
  verdict: "CONVICTED" | "ABSTAINED";
  abstention_reason: string;
  transcript: Array<{
    stage: string;
    actor: string;
    text: string;
    verified: boolean;
    claims_checked: number;
  }>;
  stage_count: number;
  sha256_digest: string;
  elapsed_ms: number;
  [key: string]: unknown;
}

/* ------------------------------ verification ------------------------------ */

export interface ChaosDrillReceipt {
  status?: string;
  iterations?: number;
  pending_after?: number;
  duplicate_transfer_ids?: number;
  stale_lease_state?: string;
  debits_micros?: number;
  credits_micros?: number;
  [key: string]: unknown;
}

export interface VoprCampaignReceipt {
  status?: string;
  seed?: number;
  steps?: number;
  faults?: Record<string, number>;
  ok?: boolean;
  [key: string]: unknown;
}

export interface OsTuneReceipt {
  status?: string;
  ring?: string;
  checks?: Array<{ check: string; status: string; detail: string; command?: string }>;
  [key: string]: unknown;
}

/* --------------------------------- misc --------------------------------- */

export interface KillReport {
  halted?: boolean;
  reason?: string;
  flatten?: boolean;
  [key: string]: unknown;
}

export interface MetricsSnapshot {
  latency?: JsonRecord;
  memory?: JsonRecord;
  [key: string]: unknown;
}
