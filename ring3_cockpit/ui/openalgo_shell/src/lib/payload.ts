/**
 * Tolerant payload readers.
 *
 * The V2.1 gateway returns typed payloads wrapped in one of three envelopes
 * (bare object, `{ok,data}`, `{payload}`).  These helpers let a workstation ask
 * for "the NAV" or "the gate rows" without hard-coding a single envelope shape.
 */

import { asArray, asRecord, deepGet, safeBool, text } from "./format";

export const ROW_KEYS = [
  "rows",
  "items",
  "records",
  "entries",
  "data",
  "results",
  "list",
  "alerts",
  "orders",
  "positions",
  "trades",
  "instruments",
  "missions",
  "tranches",
  "sagas",
  "events",
  "gates",
  "brokers",
  "sessions",
  "candidates",
  "agents",
  "factors",
  "loaders",
  "tables",
  "entities",
  "skills",
  "fills",
  "bars",
  "candles",
  "levels",
];

export function rowsOf(payload: unknown, keys: string[] = ROW_KEYS): Record<string, unknown>[] {
  if (Array.isArray(payload)) return payload.map((entry) => asRecord(entry));
  const record = asRecord(payload);
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) return value.map((entry) => asRecord(entry));
  }
  for (const key of keys) {
    const nested = asRecord(record[key]);
    for (const inner of keys) {
      if (Array.isArray(nested[inner])) return (nested[inner] as unknown[]).map((entry) => asRecord(entry));
    }
  }
  return [];
}

export function listOf(payload: unknown, keys: string[] = ROW_KEYS): unknown[] {
  if (Array.isArray(payload)) return payload;
  const record = asRecord(payload);
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

export function mapOf(payload: unknown, keys: string[] = []): Record<string, unknown> {
  const record = asRecord(payload);
  for (const key of keys) {
    const value = record[key];
    if (value !== null && typeof value === "object" && !Array.isArray(value)) return asRecord(value);
  }
  return record;
}

export function numberAt(payload: unknown, keys: string[], fallback = 0): number {
  for (const key of keys) {
    const value = deepGet(payload, key);
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim().length > 0) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return fallback;
}

export function stringAt(payload: unknown, keys: string[], fallback = ""): string {
  for (const key of keys) {
    const value = deepGet(payload, key);
    if (typeof value === "string" && value.length > 0) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return fallback;
}

export function boolAt(payload: unknown, keys: string[], fallback = false): boolean {
  for (const key of keys) {
    const value = deepGet(payload, key);
    if (typeof value === "boolean") return value;
    if (typeof value === "string") return value.toLowerCase() === "true";
  }
  return fallback;
}

const NAV_KEYS = [
  "nav_micros",
  "total_nav_micros",
  "equity_micros",
  "total_equity_micros",
  "account_value_micros",
  "portfolio_value_micros",
  "nav",
  "total_nav",
  "equity",
  "risk_telemetry.total_nav_micros",
  "risk_telemetry.nav_micros",
  "ledger.nav_micros",
];

export function navMicros(payload: unknown): number {
  return numberAt(payload, NAV_KEYS, 0);
}

export function cashMicros(payload: unknown): number {
  return numberAt(payload, ["cash_micros", "free_cash_micros", "available_micros", "risk_telemetry.free_margin_micros"], 0);
}

export function pnlMicros(payload: unknown, realized = false): number {
  return numberAt(
    payload,
    realized
      ? ["realized_pnl_micros", "risk_telemetry.realized_pnl_micros", "pnl_realized_micros"]
      : ["unrealized_pnl_micros", "risk_telemetry.unrealized_pnl_micros", "pnl_unrealized_micros"],
    0,
  );
}

export function drawdownBps(payload: unknown): number {
  return numberAt(payload, ["max_drawdown_bps", "risk_telemetry.max_drawdown_bps", "drawdown_bps"], 0);
}

export interface GateRow {
  id: string;
  status: string;
  detail: string;
}

export function gatesOf(payload: unknown): GateRow[] {
  const record = asRecord(payload);
  const rows = Array.isArray(record.gates) ? record.gates : [];
  const gates: GateRow[] = rows.map((entry, index) => {
    const row = asRecord(entry);
    return {
      id: text(row.gate ?? row.id ?? row.gate_id ?? row.name, `GATE ${index + 1}`),
      status: text(row.status ?? row.state, "UNKNOWN").toUpperCase(),
      detail: text(row.detail ?? row.message ?? row.hint ?? row.description, ""),
    };
  });
  if (gates.length > 0) return gates;
  const checklist = Array.isArray(record.gate_checklist) ? record.gate_checklist : [];
  return checklist.map((entry, index) => {
    const line = text(entry, "");
    const [status, ...rest] = line.split(" ");
    return { id: rest.join(" ") || `GATE ${index + 1}`, status: status.toUpperCase(), detail: "" };
  });
}

export interface PreflightSummary {
  passed: number;
  failed: number;
  total: number;
  ready: boolean;
  release: string;
  gates: GateRow[];
}

export function preflightOf(payload: unknown): PreflightSummary {
  return {
    passed: numberAt(payload, ["passed_gates", "gates_passed"], 0),
    failed: numberAt(payload, ["failed_gates", "gates_failed"], 0),
    total: numberAt(payload, ["total_gates", "gates_expected"], 23),
    ready: safeBool(asRecord(payload).ready_for_live_cutover, false),
    release: stringAt(payload, ["release", "schema"], ""),
    gates: gatesOf(payload),
  };
}

export function statusTone(status: unknown): "bull" | "bear" | "accent" | "muted" {
  const token = text(status, "").toUpperCase();
  if (token.includes("FAIL") || token.includes("ERROR") || token.includes("CRITICAL") || token.includes("HALT")) return "bear";
  if (token.includes("PASS") || token.includes("OK") || token.includes("READY") || token.includes("COMPLETED") || token.includes("ACTIVE") || token.includes("LIVE"))
    return "bull";
  if (token.includes("WARN") || token.includes("PENDING") || token.includes("PARTIAL") || token.includes("DEGRADED")) return "accent";
  return "muted";
}

export function pickRows<T extends Record<string, unknown>>(rows: T[], key: string, fallback = ""): string {
  for (const row of rows) {
    const value = row[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return fallback;
}

export function numericColumn(rows: Record<string, unknown>[], keys: string[]): number[] {
  return rows.map((row) => numberAt(row, keys, 0));
}

export function safeArrayRows(payload: unknown, key: string): Record<string, unknown>[] {
  const record = asRecord(payload);
  return asArray(record[key]).map((entry) => asRecord(entry));
}

export function totalOf(rows: Record<string, unknown>[], keys: string[]): number {
  return rows.reduce((sum, row) => sum + numberAt(row, keys, 0), 0);
}
