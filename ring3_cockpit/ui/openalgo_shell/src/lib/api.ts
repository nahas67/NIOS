/**
 * V1.5 gateway client.
 *
 * One request pipeline for every `/api/v2/*` route: JSON in, JSON out, the
 * sovereign session token mirrored on `X-NIOS-Session` so an XHR still works
 * when a strict cookie policy blocks storage, and a typed error that keeps the
 * server's `detail` string intact for the operator.
 */

import type { AuthSession, AuthStatus, JsonRecord } from "./types";

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

/* ---------------------------------- auth ---------------------------------- */

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

  paperState: () => request<JsonRecord>("/api/v2/workspace/paper/state"),

  liveState: () => request<JsonRecord>("/api/v2/workspace/live/state"),

  preflight: () => request<JsonRecord>("/api/v2/control/preflight"),
};

/* -------------------------------- read models ------------------------------ */

export const api = {
  state: () => request<JsonRecord>("/api/v2/cockpit/state"),

  formalProofs: (refresh = false) =>
    request<JsonRecord>(`/api/v2/cockpit/formal-proofs${refresh ? "?refresh=true" : ""}`),

  pages: () => request<JsonRecord>("/api/v2/cockpit/pages"),

  terminal: () => request<JsonRecord>("/api/v2/cockpit/terminal"),

  terminalQuote: (payload: { symbol?: string; silo?: string; bars?: number; depth?: number }) =>
    post<JsonRecord>("/api/v2/control/terminal/quote", payload),

  orders: () => request<JsonRecord>("/api/v2/cockpit/orders"),

  options: () => request<JsonRecord>("/api/v2/cockpit/options"),

  optionsChain: (payload: {
    symbol?: string;
    spot?: number;
    volatility?: number;
    days_to_expiry?: number;
    strike_count?: number;
    risk_free_rate?: number;
  }) => post<JsonRecord>("/api/v2/control/options/chain", payload),

  strategyPayoff: (payload: {
    symbol?: string;
    strategy: string;
    spot?: number;
    volatility?: number;
    days_to_expiry?: number;
    width_steps?: number;
    risk_free_rate?: number;
  }) => post<JsonRecord>("/api/v2/control/options/strategy-payoff", payload),

  analyzer: () => request<JsonRecord>("/api/v2/cockpit/analyzer"),

  quant: (refresh = false) =>
    request<JsonRecord>(`/api/v2/cockpit/quant${refresh ? "?refresh=true" : ""}`),

  priceOption: (payload: {
    spot: number;
    strike: number;
    volatility: number;
    days_to_expiry: number;
    risk_free_rate?: number;
    kind?: "CALL" | "PUT";
    [key: string]: unknown;
  }) => post<JsonRecord>("/api/v2/control/quant/price-option", payload),

  quantScan: (payload: { top_k?: number; universe_size?: number } = { top_k: 15 }) =>
    post<JsonRecord>("/api/v2/control/quant/scan", payload),

  agents: () => request<JsonRecord>("/api/v2/cockpit/agents"),

  assignAgent: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/agents/assign", { ...payload }),

  batchWatchlist: (payload: { silo: string; symbols: string[]; strategy_id?: string | null }) =>
    post<JsonRecord>("/api/v2/control/agents/batch-watchlist", { ...payload }),

  agentFollowup: (payload: { task_id: string; question: string }) =>
    post<JsonRecord>("/api/v2/control/agents/followup", { ...payload }),

  cancelAgentTasks: (taskId = "ALL") =>
    post<JsonRecord>("/api/v2/control/agents/cancel", { task_id: taskId }),

  backtest: () => request<JsonRecord>("/api/v2/cockpit/backtest"),

  runBacktest: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/backtest/run", { ...payload }),

  compareStrategies: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/backtest/compare-strategies", { ...payload }),

  promoteToNursery: (backtestId: string) =>
    post<JsonRecord>("/api/v2/control/backtest/promote-to-nursery", {
      backtest_id: backtestId,
    }),

  brain: () => request<JsonRecord>("/api/v2/cockpit/brain"),

  mirofishShock: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/brain/mirofish-shock", { ...payload }),

  committeeDebate: (payload: { silo?: string; symbol?: string; spread_bps?: number }) =>
    post<JsonRecord>("/api/v2/control/brain/committee-debate", { ...payload }),

  forge: () => request<JsonRecord>("/api/v2/cockpit/forge"),

  evaluateChallenger: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/forge/evaluate-challenger", { ...payload }),

  forgeHedge: () =>
    post<JsonRecord>("/api/v2/control/supervisor/forge-hedge", { operator: "ring3.cockpit" }),

  skillMarkdown: (name: string) =>
    request<JsonRecord>(`/api/v2/cockpit/skills/${encodeURIComponent(name)}`),

  ledger: () => request<JsonRecord>("/api/v2/cockpit/ledger"),

  reconcile: (silos?: string[]) =>
    post<JsonRecord>("/api/v2/control/ledger/reconcile", {
      silos: silos ?? null,
      operator: "ring3.cockpit",
    }),

  logbook: () => request<JsonRecord>("/api/v2/cockpit/logbook"),

  logbookEntry: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/logbook/entry", { ...payload }),

  clearLogbook: () => post<JsonRecord>("/api/v2/control/logbook/clear"),

  verification: () => request<JsonRecord>("/api/v2/cockpit/verification"),

  chaosDrill: (payload: JsonRecord) =>
    post<JsonRecord>("/api/v2/control/verification/chaos-drill", { ...payload }),

  vopr: (payload: { seed?: number; steps?: number }) =>
    post<JsonRecord>("/api/v2/control/verification/vopr", { ...payload }),

  osTune: (payload: { apply?: boolean; ring?: string }) =>
    post<JsonRecord>("/api/v2/control/verification/os-tune", { ...payload }),

  brokers: () => request<JsonRecord>("/api/v2/cockpit/brokers"),

  brokerPing: (venue: string) =>
    post<JsonRecord>("/api/v2/control/brokers/ping", { venue, operator: "ring3.cockpit" }),

  brokerCredentials: (payload: {
    silo: string;
    venue: string;
    api_key: string;
    api_secret: string;
  }) => post<JsonRecord>("/api/v2/control/brokers/credentials", { sandbox: true, ...payload }),

  mcp: () => request<JsonRecord>("/api/v2/cockpit/mcp"),

  mcpCall: (name: string, args: Record<string, unknown> = {}) =>
    post<JsonRecord>("/api/v2/mcp/call", { name, arguments: args }),

  preflight: (refresh = false) =>
    request<JsonRecord>(`/api/v2/control/preflight${refresh ? "?refresh=true" : ""}`),

  runPreflight: (fullSmoke = false, silo = "ALL") =>
    post<JsonRecord>("/api/v2/control/preflight", {
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

  llmProbe: () => post<JsonRecord>("/api/v2/control/llms/probe", { operator: "ring3.cockpit" }),

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

  metrics: () => request<JsonRecord>("/api/v2/metrics/json"),

  killSwitch: (reason = "operator kill switch") =>
    post<JsonRecord>("/api/v2/control/killswitch", { reason, flatten: true, confirm: true }),

  rearm: (reason = "cockpit re-arm") =>
    post<JsonRecord>("/api/v2/control/rearm", { reason, operator: "ring3.cockpit" }),

  extensions: () => request<JsonRecord>("/api/v2/control/extensions"),
} as const;

/* ------------------------------- order actions ----------------------------- */

export function postPaperOrder(payload: JsonRecord): Promise<JsonRecord> {
  return post<JsonRecord>("/api/v2/control/orders/paper", {
    operator: "ring3.cockpit",
    ...payload,
  });
}

export function postCancelOrders(orderId = "ALL"): Promise<JsonRecord> {
  return post<JsonRecord>("/api/v2/control/orders/cancel", {
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
