/** Minimal cross-cutting types for the V1.5 terminal. */

export type JsonRecord = Record<string, unknown>;

export type Tone = "neutral" | "bull" | "bear" | "accent" | "muted";

export type Tier = "NONE" | "PAPER" | "LIVE";

export type Workspace = "base" | "paper" | "live";

export interface AuthSession {
  token: string;
  username: string;
  tier: Tier;
  workspace?: Workspace | string;
  expires_ns?: number | null;
}

export interface AuthStatus {
  authenticated: boolean;
  initialized?: boolean;
  username?: string | null;
  tier?: Tier;
  workspace?: string | null;
  live_pin_configured?: boolean;
  session_ttl_seconds?: number;
}

export interface PaperWorkspaceState {
  clean_slate_active: boolean;
  autopilot: { enabled: boolean };
  shadow: { shadow_fills: number; required_fills: number; graduated: boolean };
  orders: { working: JsonRecord[] };
  silos: Record<string, { nav_micros: number }>;
  [key: string]: unknown;
}

export interface LiveWorkspaceState {
  tier?: Tier;
  expires_ns?: number | null;
  [key: string]: unknown;
}

export interface PreflightGate {
  id: string;
  title?: string;
  status: string;
  detail?: string;
}

export interface PreflightCertificate {
  passed_gates?: number;
  total_gates?: number;
  gates?: PreflightGate[];
  certificate?: { gates?: PreflightGate[] };
  [key: string]: unknown;
}

/**
 * Cockpit read models are gateway-shaped JSON. Pages read them through the
 * tolerant `pick`/`rows` helpers in `format.ts` rather than mirroring every
 * server field here, so a additive server change can never break the build.
 */
export type PagePayload = JsonRecord;
