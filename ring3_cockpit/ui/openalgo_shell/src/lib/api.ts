/**
 * V1.4 typed gateway client.
 *
 * One request pipeline for every `/api/v2/*` route: JSON in, JSON out, the
 * sovereign session token mirrored on `X-NIOS-Session` so an XHR still works
 * when a strict cookie policy blocks storage, and a typed error that keeps the
 * server's `detail` string intact for the operator.
 */

import type {
  AgentAssignPayload,
  AgentAssignReceipt,
  AgentBatchWatchlistReceipt,
  AgentFollowupReceipt,
  AgentsPageData,
  AnalyzerPageData,
  AuthSession,
  AuthStatus,
  BacktestComparisonReceipt,
  BacktestPageData,
  BacktestReceipt,
  BacktestTimeframe,
  BrokerPingReceipt,
  BrokersPageData,
  CancelOrdersReceipt,
  ChallengerEvalReceipt,
  ChaosDrillReceipt,
  CockpitState,
  CommitteeDebateReceipt,
  FormalProofState,
  ForgePageData,
  InteractiveQuantScan,
  JsonRecord,
  KillReport,
  LedgerPageData,
  LedgerReconcileReceipt,
  LiveWorkspaceState,
  LogbookEntryPayload,
  LogbookEntryRecord,
  LogbookPageData,
  McpCallResult,
  McpPageData,
  MetricsSnapshot,
  MiroFishShockReceipt,
  NurseryPromotionReceipt,
  OptionPriceReceipt,
  OptionsChainReceipt,
  OptionsPageData,
  OrdersPageData,
  OsTuneReceipt,
  PagesIndex,
  PaperOrderPayload,
  PaperOrderReceipt,
  PaperWorkspaceState,
  PreflightCertificate,
  QuantPageData,
  SkillMarkdownReceipt,
  StrategyPayoffReceipt,
  TerminalFrame,
  TerminalPageData,
  VoprCampaignReceipt,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const TOKEN_KEY = "nios.session.v1";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function storedToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setStoredToken(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage disabled: the HttpOnly cookie is still authoritative */
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = storedToken();
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-NIOS-Session": token } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const body = await response.text();
  if (!response.ok) {
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
      else if (Array.isArray(parsed.detail)) detail = JSON.stringify(parsed.detail);
    } catch {
      /* keep the raw body */
    }
    throw new ApiError(detail || `HTTP ${response.status}`, response.status);
  }
  return (body ? JSON.parse(body) : {}) as T;
}

function post<T>(path: string, payload: JsonRecord = {}): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(payload) });
}

export function streamUrl(): string {
  return `${API_BASE}/api/v2/cockpit/stream`;
}

export function metricsUrl(): string {
  return `${API_BASE}/api/v2/metrics`;
}

/* ------------------------------- auth ------------------------------- */

export const authApi = {
  status: () => request<AuthStatus>("/api/v2/auth/status"),

  setup: (payload: { username: string; password: string; live_pin: string }) =>
    post<AuthSession>("/api/v2/auth/setup", { ...payload, start_clean_slate: true }),

  login: (payload: { username: string; password: string }) =>
    post<AuthSession>("/api/v2/auth/login", payload),

  loginPaper: (payload: { username: string; password: string }) =>
    post<AuthSession>("/api/v2/auth/login/paper", payload),

  loginLive: (payload: { username: string; password: string; live_pin: string }) =>
    post<AuthSession>("/api/v2/auth/login/live", payload),

  elevate: (livePin: string) =>
    post<AuthSession>("/api/v2/auth/elevate-live", { live_pin: livePin }),

  logout: () => post<{ ok: boolean; revoked: boolean }>("/api/v2/auth/logout"),

  resetEverything: (payload: { password: string; live_pin: string }) =>
    post<JsonRecord>("/api/v2/auth/reset", payload),

  resetWorkspace: () => post<JsonRecord>("/api/v2/control/workspace/reset"),

  fundPaper: (payload: { silo: string; amount_units: number; vault?: string }) =>
    post<JsonRecord>("/api/v2/control/ledger/fund-paper", payload),

  setAutopilot: (enabled: boolean) =>
    post<{ ok: boolean; autopilot: { enabled: boolean } }>(
      "/api/v2/control/workspace/autopilot",
      { enabled },
    ),

  paperState: () => request<PaperWorkspaceState>("/api/v2/workspace/paper/state"),

  liveState: () => request<LiveWorkspaceState>("/api/v2/workspace/live/state"),

  preflight: () => request<PreflightCertificate>("/api/v2/control/preflight"),
};

/* ------------------------------ read models ------------------------------ */

export const api = {
  state: () => request<CockpitState>("/api/v2/cockpit/state"),

  formalProofs: (refresh = false) =>
    request<FormalProofState>(`/api/v2/cockpit/formal-proofs${refresh ? "?refresh=true" : ""}`),

  pages: () => request<PagesIndex>("/api/v2/cockpit/pages"),

  terminal: () => request<TerminalPageData>("/api/v2/cockpit/terminal"),

  terminalQuote: (payload: { symbol?: string; silo?: string; bars?: number; depth?: number }) =>
    post<TerminalFrame>("/api/v2/control/terminal/quote", payload),

  orders: () => request<OrdersPageData>("/api/v2/cockpit/orders"),

  options: () => request<OptionsPageData>("/api/v2/cockpit/options"),

  optionsChain: (payload: {
    symbol?: string;
    spot?: number;
    volatility?: number;
    days_to_expiry?: number;
    strike_count?: number;
    risk_free_rate?: number;
  }) => post<OptionsChainReceipt>("/api/v2/control/options/chain", payload),

  strategyPayoff: (payload: {
    symbol?: string;
    strategy: string;
    spot?: number;
    volatility?: number;
    days_to_expiry?: number;
    width_steps?: number;
    risk_free_rate?: number;
  }) => post<StrategyPayoffReceipt>("/api/v2/control/options/strategy-payoff", payload),

  analyzer: () => request<AnalyzerPageData>("/api/v2/cockpit/analyzer"),

  quant: (refresh = false) =>
    request<QuantPageData>(`/api/v2/cockpit/quant${refresh ? "?refresh=true" : ""}`),

  priceOption: (payload: {
    spot: number;
    strike: number;
    volatility: number;
    days_to_expiry: number;
    risk_free_rate?: number;
    kind?: "CALL" | "PUT";
    [key: string]: unknown;
  }) => post<OptionPriceReceipt>("/api/v2/control/quant/price-option", payload),

  quantScan: (payload: { top_k?: number; universe_size?: number } = { top_k: 15 }) =>
    post<InteractiveQuantScan>("/api/v2/control/quant/scan", payload),

  agents: () => request<AgentsPageData>("/api/v2/cockpit/agents"),

  assignAgent: (payload: AgentAssignPayload) =>
    post<AgentAssignReceipt>("/api/v2/control/agents/assign", { ...payload }),

  batchWatchlist: (payload: { silo: string; symbols: string[]; strategy_id?: string | null }) =>
    post<AgentBatchWatchlistReceipt>("/api/v2/control/agents/batch-watchlist", { ...payload }),

  agentFollowup: (payload: { task_id: string; question: string }) =>
    post<AgentFollowupReceipt>("/api/v2/control/agents/followup", { ...payload }),

  cancelAgentTasks: (taskId = "ALL") =>
    post<JsonRecord>("/api/v2/control/agents/cancel", { task_id: taskId }),

  backtest: () => request<BacktestPageData>("/api/v2/cockpit/backtest"),

  runBacktest: (payload: {
    silo: string;
    symbol: string;
    strategy_id: string;
    bars: number;
    timeframe: BacktestTimeframe;
    initial_capital_units: number;
    fast_window?: number | null;
    slow_window?: number | null;
    stop_loss_bps?: number | null;
    take_profit_bps?: number | null;
  }) => post<BacktestReceipt>("/api/v2/control/backtest/run", { ...payload }),

  compareStrategies: (payload: {
    silo: string;
    symbol: string;
    timeframe: BacktestTimeframe;
    bars: number;
  }) => post<BacktestComparisonReceipt>("/api/v2/control/backtest/compare-strategies", { ...payload }),

  promoteToNursery: (backtestId: string) =>
    post<NurseryPromotionReceipt>("/api/v2/control/backtest/promote-to-nursery", {
      backtest_id: backtestId,
    }),

  brain: () => request<JsonRecord>("/api/v2/cockpit/brain"),

  mirofishShock: (payload: {
    symbol?: string;
    shock_bps?: number;
    sentiment_tag?: string;
    volatility_ratio?: number;
    spread_bps?: number;
    gamma_flip_proximity?: number;
    silo?: string;
  }) => post<MiroFishShockReceipt>("/api/v2/control/brain/mirofish-shock", { ...payload }),

  committeeDebate: (payload: { silo?: string; symbol?: string; spread_bps?: number }) =>
    post<CommitteeDebateReceipt>("/api/v2/control/brain/committee-debate", { ...payload }),

  forge: () => request<ForgePageData>("/api/v2/cockpit/forge"),

  evaluateChallenger: (payload: {
    plugin_name: string;
    candidate_sharpe?: number;
    shadow_fills?: number;
    silo?: string;
  }) => post<ChallengerEvalReceipt>("/api/v2/control/forge/evaluate-challenger", { ...payload }),

  forgeHedge: () =>
    post<JsonRecord>("/api/v2/control/supervisor/forge-hedge", { operator: "ring3.cockpit" }),

  skillMarkdown: (name: string) =>
    request<SkillMarkdownReceipt>(`/api/v2/cockpit/skills/${encodeURIComponent(name)}`),

  ledger: () => request<LedgerPageData>("/api/v2/cockpit/ledger"),

  reconcile: (silos?: string[]) =>
    post<LedgerReconcileReceipt>("/api/v2/control/ledger/reconcile", {
      silos: silos ?? null,
      operator: "ring3.cockpit",
    }),

  logbook: () => request<LogbookPageData>("/api/v2/cockpit/logbook"),

  logbookEntry: (payload: LogbookEntryPayload) =>
    post<{ ok: boolean; entry: LogbookEntryRecord; aggregate: LogbookPageData["aggregate"] }>(
      "/api/v2/control/logbook/entry",
      { ...payload },
    ),

  clearLogbook: () => post<JsonRecord>("/api/v2/control/logbook/clear"),

  verification: () => request<JsonRecord>("/api/v2/cockpit/verification"),

  chaosDrill: (payload: {
    iterations?: number;
    hold_ms?: number;
    seed?: number;
    silo?: string;
    symbol?: string;
  }) => post<ChaosDrillReceipt>("/api/v2/control/verification/chaos-drill", { ...payload }),

  vopr: (payload: { seed?: number; steps?: number }) =>
    post<VoprCampaignReceipt>("/api/v2/control/verification/vopr", { ...payload }),

  osTune: (payload: { apply?: boolean; ring?: string }) =>
    post<OsTuneReceipt>("/api/v2/control/verification/os-tune", { ...payload }),

  brokers: () => request<BrokersPageData>("/api/v2/cockpit/brokers"),

  brokerPing: (venue: string) =>
    post<BrokerPingReceipt>("/api/v2/control/brokers/ping", {
      venue,
      operator: "ring3.cockpit",
    }),

  brokerCredentials: (payload: {
    silo: string;
    venue: string;
    api_key: string;
    api_secret: string;
  }) => post<JsonRecord>("/api/v2/control/brokers/credentials", { sandbox: true, ...payload }),

  mcp: () => request<McpPageData>("/api/v2/cockpit/mcp"),

  mcpCall: (name: string, args: Record<string, unknown> = {}) =>
    post<McpCallResult>("/api/v2/mcp/call", { name, arguments: args }),

  preflight: (refresh = false) =>
    request<PreflightCertificate>(`/api/v2/control/preflight${refresh ? "?refresh=true" : ""}`),

  runPreflight: (fullSmoke = false, silo = "ALL") =>
    post<PreflightCertificate>("/api/v2/control/preflight", {
      full_smoke: fullSmoke,
      silo,
      refresh: true,
    }),

  vaultStatus: () => request<JsonRecord>("/api/v2/control/vault/status"),

  rotateKeys: () =>
    post<JsonRecord>("/api/v2/control/vault/rotate-keys", {
      operator: "ring3.cockpit",
      reason: "operator-initiated rotation",
    }),

  llmProbe: () =>
    post<JsonRecord>("/api/v2/control/llms/probe", { operator: "ring3.cockpit" }),

  supervisor: (action: "start" | "stop" | "restart_ring" | "status", ringIndex?: number) =>
    post<JsonRecord>("/api/v2/control/supervisor", {
      action,
      ring_index: ringIndex ?? null,
      operator: "ring3.cockpit",
    }),

  feedMode: (feedMode: string, ingestCycle = true) =>
    post<JsonRecord>("/api/v2/control/feeds", {
      feed_mode: feedMode,
      ingest_cycle: ingestCycle,
      operator: "ring3.cockpit",
    }),

  metrics: () => request<MetricsSnapshot>("/api/v2/metrics/json"),

  killSwitch: (reason = "operator kill switch") =>
    post<KillReport>("/api/v2/control/killswitch", { reason, flatten: true, confirm: true }),

  rearm: (reason = "cockpit re-arm") =>
    post<JsonRecord>("/api/v2/control/rearm", {
      reason,
      operator: "ring3.cockpit",
    }),

  extensions: () => request<JsonRecord>("/api/v2/control/extensions"),
} as const;

/* ------------------------------ order actions ------------------------------ */

export function postPaperOrder(payload: PaperOrderPayload): Promise<PaperOrderReceipt> {
  return post<PaperOrderReceipt>("/api/v2/control/orders/paper", {
    operator: "ring3.cockpit",
    ...payload,
  });
}

export function postCancelOrders(orderId = "ALL"): Promise<CancelOrdersReceipt> {
  return post<CancelOrdersReceipt>("/api/v2/control/orders/cancel", {
    order_id: orderId,
    operator: "ring3.cockpit",
  });
}

export function postShadowBurst(payload: {
  silo: string;
  fills?: number;
  strategy_id?: string;
}): Promise<JsonRecord> {
  return post<JsonRecord>("/api/v2/control/analyzer/shadow-burst", {
    operator: "ring3.cockpit",
    ...payload,
  });
}
