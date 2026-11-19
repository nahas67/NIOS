/**
 * NIOS V2.2 hardened API client.
 *
 * Contract (mirrors `ring3_cockpit/gateway/v2_1_control.py`):
 *  - `GET /api/v2/auth/session` returns the live operator state plus the
 *    session-bound `csrf_token` = sha256("nios-csrf-v1:" + session)[:32].
 *  - Every mutating request MUST echo `X-NIOS-CSRF` when the browser is riding
 *    the HttpOnly `nios_session` cookie, and every request carries
 *    `credentials: "include"`.
 *  - `X-NIOS-Session` is forwarded whenever the login portal hands the client
 *    an explicit session token.
 */

import { asRecord, pick, safeBool, safeNumber, text } from "./format";

export const API_ROOT = "/api/v2";

/** Server-side clamps (reject client-side before submission). */
export const LIMITS = {
  barsMin: 1,
  barsMax: 5000,
  sagasMin: 1,
  sagasMax: 1000,
  depthMax: 50,
  chainStrikesMax: 31,
} as const;

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue | QueryValue[]>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;

  constructor(message: string, status = 0, code = "API_ERROR", detail = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail || message;
  }
}

const SESSION_KEY = "nios.session.token";
const CSRF_KEY = "nios.csrf.token";

function readStore(key: string): string | null {
  try {
    return globalThis.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string | null): void {
  try {
    if (value && value.length > 0) globalThis.sessionStorage.setItem(key, value);
    else globalThis.sessionStorage.removeItem(key);
  } catch {
    /* storage disabled — cookie transport still works */
  }
}

let sessionToken: string | null = readStore(SESSION_KEY);
let csrfToken: string | null = readStore(CSRF_KEY);

export function setTokens(session?: string | null, csrf?: string | null): void {
  if (session !== undefined) {
    sessionToken = session;
    writeStore(SESSION_KEY, session);
  }
  if (csrf !== undefined) {
    csrfToken = csrf;
    writeStore(CSRF_KEY, csrf);
  }
}

export function getSessionToken(): string | null {
  return sessionToken;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function clearTokens(): void {
  sessionToken = null;
  csrfToken = null;
  writeStore(SESSION_KEY, null);
  writeStore(CSRF_KEY, null);
}

export function assertFinite(value: unknown, label: string): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) {
    throw new ApiError(`${label} must be a finite number`, 0, "CLIENT_NON_FINITE", `${label}=${String(value)}`);
  }
  return n;
}

export function clampLimit(value: unknown, min: number, max: number, fallback: number): number {
  if (value === undefined || value === null || value === "") return fallback;
  const n = assertFinite(value, "limit");
  return Math.min(max, Math.max(min, Math.trunc(n)));
}

export function clampBars(value: unknown, fallback = 240): number {
  return clampLimit(value, LIMITS.barsMin, LIMITS.barsMax, fallback);
}

export function clampSagas(value: unknown, fallback = 100): number {
  return clampLimit(value, LIMITS.sagasMin, LIMITS.sagasMax, fallback);
}

function buildQuery(params?: QueryParams): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const entry of value) {
        if (entry === undefined || entry === null || entry === "") continue;
        search.append(key, String(entry));
      }
      continue;
    }
    search.append(key, String(value));
  }
  const encoded = search.toString();
  return encoded.length > 0 ? `?${encoded}` : "";
}

/** Peel the NIOS payload envelope, then a `{ok, data}` envelope, then raw JSON. */
function unwrap<T>(json: unknown): T {
  const record = asRecord(json);
  if (Object.keys(record).length === 0) return json as T;

  const payload = record.payload;
  if (payload !== undefined && payload !== null && typeof payload === "object" && !Array.isArray(payload)) {
    const inner = asRecord(payload);
    if ("data" in inner) return inner.data as T;
    return payload as T;
  }

  if ("ok" in record && "data" in record) return record.data as T;
  return json as T;
}

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

interface RequestOptions {
  params?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const url = `${API_ROOT}${path}${buildQuery(options.params)}`;
  const headers: Record<string, string> = { Accept: "application/json" };

  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (sessionToken) headers["X-NIOS-Session"] = sessionToken;
  if (MUTATING_METHODS.has(method) && csrfToken) headers["X-NIOS-CSRF"] = csrfToken;

  const controller = new AbortController();
  const timeout = options.timeoutMs ?? 30_000;
  const timer = globalThis.setTimeout(() => controller.abort(), timeout);
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      credentials: "include",
      headers,
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
  } catch (error) {
    throw new ApiError(`gateway unreachable: ${String(error)}`, 0, "NETWORK_ERROR", String(error));
  } finally {
    globalThis.clearTimeout(timer);
  }

  if (response.status === 204) return undefined as T;

  const raw = await response.text();
  let json: unknown = null;
  if (raw.length > 0) {
    try {
      json = JSON.parse(raw);
    } catch {
      json = null;
    }
  }

  if (!response.ok) {
    const body = asRecord(json);
    const detail = text(body.detail ?? body.error ?? body.hint, `HTTP ${response.status}`);
    const code = text(body.code ?? body.detail ?? "HTTP_ERROR", "HTTP_ERROR");
    throw new ApiError(detail, response.status, code, detail);
  }

  return unwrap<T>(json);
}

export const api = {
  get<T>(path: string, params?: QueryParams, signal?: AbortSignal): Promise<T> {
    return request<T>("GET", path, { params, signal });
  },
  post<T>(path: string, body?: unknown, params?: QueryParams): Promise<T> {
    return request<T>("POST", path, { body: body ?? {}, params });
  },
  patch<T>(path: string, body?: unknown): Promise<T> {
    return request<T>("PATCH", path, { body: body ?? {} });
  },
  del<T>(path: string, body?: unknown): Promise<T> {
    return request<T>("DELETE", path, { body });
  },
};

export type WorkspaceMode = "PAPER" | "LIVE" | "NONE";

export interface SessionState {
  authenticated: boolean;
  initialized: boolean;
  mode: WorkspaceMode;
  username: string | null;
  ttlRemainingS: number;
  expiresAtUtc: string | null;
  issuedAtUtc: string | null;
  cleanSlate: boolean;
  workspace: string | null;
  csrfToken: string | null;
  sessionToken: string | null;
  raw: Record<string, unknown>;
}

function normalizeMode(value: unknown): WorkspaceMode {
  const token = text(value, "NONE").toUpperCase();
  if (token.includes("LIVE")) return "LIVE";
  if (token.includes("PAPER")) return "PAPER";
  return "NONE";
}

/** Hydrate the live operator state from `GET /api/v2/auth/session`. */
export async function fetchSession(signal?: AbortSignal): Promise<SessionState> {
  const data = await api.get<unknown>("/auth/session", undefined, signal);
  const record = asRecord(data);

  const csrf = text(record.csrf_token, "") || null;
  const session = text(record.session_token, "") || null;
  if (csrf) setTokens(undefined, csrf);
  if (session) setTokens(session, undefined);

  const ttl = safeNumber(
    record.ttl_remaining_s ??
      record.ttl_remaining ??
      record.session_ttl_s ??
      record.expires_in_s,
    0,
  );

  return {
    authenticated: safeBool(record.authenticated, false),
    initialized: safeBool(record.initialized ?? record.setup_complete ?? record.operator_present, false),
    mode: normalizeMode(record.mode ?? record.tier ?? record.workspace_mode),
    username: text(record.username ?? record.operator, "") || null,
    ttlRemainingS: ttl,
    expiresAtUtc: text(record.expires_at_utc ?? record.expires_at, "") || null,
    issuedAtUtc: text(record.issued_at_utc ?? record.issued_at_ns, "") || null,
    cleanSlate: safeBool(record.clean_slate_active ?? record.clean_slate, true),
    workspace: text(record.workspace, "") || null,
    csrfToken: csrf,
    sessionToken: session,
    raw: record,
  };
}

export interface AuthResult {
  ok: boolean;
  message: string;
  session: SessionState | null;
}

export async function readSessionAfterAuth(): Promise<SessionState | null> {
  try {
    return await fetchSession();
  } catch {
    return null;
  }
}

/** `POST /api/v2/auth/setup` — first-run sovereign operator. */
export async function authSetup(input: {
  username: string;
  password: string;
  livePin: string;
  startCleanSlate?: boolean;
}): Promise<AuthResult> {
  await api.post("/auth/setup", {
    username: input.username,
    password: input.password,
    live_pin: input.livePin,
    start_clean_slate: input.startCleanSlate ?? true,
  });
  return { ok: true, message: "SOVEREIGN OPERATOR PROVISIONED", session: await readSessionAfterAuth() };
}

/** `POST /api/v2/auth/login/paper` — two-factor PAPER portal. */
export async function authLoginPaper(username: string, password: string): Promise<AuthResult> {
  await api.post("/auth/login/paper", { username, password });
  return { ok: true, message: "PAPER SANDBOX SESSION MINTED", session: await readSessionAfterAuth() };
}

/** `POST /api/v2/auth/login/live` — three-factor LIVE portal. */
export async function authLoginLive(username: string, password: string, livePin: string): Promise<AuthResult> {
  await api.post("/auth/login/live", { username, password, live_pin: livePin });
  return { ok: true, message: "LIVE SOVEREIGN VAULT UNSEALED", session: await readSessionAfterAuth() };
}

/** `POST /api/v2/auth/elevate-live` — PIN elevation on an existing session. */
export async function authElevateLive(livePin: string): Promise<AuthResult> {
  await api.post("/auth/elevate-live", { live_pin: livePin });
  return { ok: true, message: "SESSION ELEVATED TO LIVE", session: await readSessionAfterAuth() };
}

export async function authLogout(): Promise<void> {
  try {
    await api.post("/auth/logout", {});
  } finally {
    clearTokens();
  }
}

export async function authReset(password: string, livePin: string): Promise<void> {
  await api.post("/auth/reset", { password, live_pin: livePin });
  clearTokens();
}

export interface AlertRecord {
  id: string;
  severity: string;
  title: string;
  message: string;
  acknowledged: boolean;
  createdAt: string;
  raw: Record<string, unknown>;
}

export function normalizeAlerts(payload: unknown): AlertRecord[] {
  const record = asRecord(payload);
  const source = Array.isArray(payload)
    ? payload
    : (record.alerts ?? record.items ?? record.rows ?? record.events ?? []);
  if (!Array.isArray(source)) return [];
  return source.filter((entry) => entry !== null && typeof entry === "object").map((entry) => {
    const row = asRecord(entry);
    return {
      id: text(row.alert_id ?? row.id ?? row.key, "ALERT"),
      severity: text(row.severity ?? row.level ?? row.tier, "INFO").toUpperCase(),
      title: text(row.title ?? row.code ?? row.kind, "OPERATOR ALERT"),
      message: text(row.message ?? row.detail ?? row.description, ""),
      acknowledged: safeBool(row.acknowledged ?? row.acked ?? row.resolved, false),
      createdAt: text(row.created_at_utc ?? row.created_at ?? row.ts_utc, ""),
      raw: row,
    };
  });
}

export async function fetchAlerts(limit = 100): Promise<AlertRecord[]> {
  const payload = await api.get<unknown>("/alerts", { limit });
  return normalizeAlerts(payload);
}

export function isCriticalAlert(alert: AlertRecord): boolean {
  const token = `${alert.severity} ${alert.title}`.toUpperCase();
  return (
    token.includes("CRITICAL") ||
    token.includes("MANUAL_INTERVENTION_REQUIRED") ||
    token.includes("MANUAL INTERVENTION")
  );
}

export const UNACKNOWLEDGED = (alerts: AlertRecord[]): number =>
  alerts.filter((alert) => !alert.acknowledged).length;

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** Session token echo for UI diagnostics. */
export function sessionFingerprint(): string {
  if (!sessionToken) return "COOKIE";
  return `${sessionToken.slice(0, 6)}…${sessionToken.slice(-4)}`;
}

export const SESSION_TTL_KEY = "ttl_remaining_s";
export const SESSION_EXPIRES_KEY = "expires_at_utc";
export const SESSION_MODE_KEY = "mode";
export const SESSION_USERNAME_KEY = "username";

export function describeSession(session: SessionState | null): string {
  if (!session) return "NO SESSION";
  if (!session.authenticated) return "UNAUTHENTICATED";
  return `${session.mode} :: ${pick(session.raw, [SESSION_USERNAME_KEY], "operator")}`;
}
