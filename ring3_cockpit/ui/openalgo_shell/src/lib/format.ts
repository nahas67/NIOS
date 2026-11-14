/**
 * Numeric, temporal and JSON-shape helpers.
 *
 * The gateway read models are additive JSON, so every page reads them through
 * these tolerant accessors: a missing field yields a zero/empty state instead of
 * a runtime crash, which is exactly what a clean-slate install renders.
 */

import type { JsonRecord, Tone } from "./types";

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Resolve a dotted path (`silos.INDIA.nav_micros`) inside an unknown payload. */
export function pick(source: unknown, path: string): unknown {
  let cursor: unknown = source;
  for (const segment of path.split(".")) {
    if (isRecord(cursor)) {
      cursor = cursor[segment];
      continue;
    }
    if (Array.isArray(cursor) && /^\d+$/.test(segment)) {
      cursor = cursor[Number(segment)];
      continue;
    }
    return undefined;
  }
  return cursor;
}

export function str(source: unknown, path: string, fallback = ""): string {
  const value = pick(source, path);
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

export function num(source: unknown, path: string, fallback = 0): number {
  const value = pick(source, path);
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return fallback;
}

export function bool(source: unknown, path: string, fallback = false): boolean {
  const value = pick(source, path);
  return typeof value === "boolean" ? value : fallback;
}

export function rows(source: unknown, path: string): JsonRecord[] {
  const value = pick(source, path);
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function strings(source: unknown, path: string): string[] {
  const value = pick(source, path);
  if (!Array.isArray(value)) return [];
  return value.map((item) => (typeof item === "string" ? item : String(item)));
}

/* ------------------------------- money --------------------------------- */

/** IBOR stores micro-units in 128-bit integers; render them as a currency. */
export function money(micros: number, currency = "USD", digits = 2): string {
  const units = micros / 1_000_000;
  return `${currency} ${units.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function moneySigned(micros: number, currency = "USD", digits = 2): string {
  const sign = micros > 0 ? "+" : micros < 0 ? "-" : "";
  return `${sign}${money(Math.abs(micros), currency, digits)}`;
}

export function moneyCompact(micros: number, currency = "USD"): string {
  const units = micros / 1_000_000;
  const abs = Math.abs(units);
  if (abs >= 1_000_000_000) return `${currency} ${(units / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${currency} ${(units / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${currency} ${(units / 1_000).toFixed(2)}K`;
  return `${currency} ${units.toFixed(2)}`;
}

/* ------------------------------ quantities ------------------------------ */

export function number(value: number, digits = 2): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function integer(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

export function pct(value: number, digits = 2): string {
  return `${value.toFixed(digits)}%`;
}

export function signedPct(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function signed(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}

export function bps(value: number, digits = 1): string {
  return `${signed(value, digits)} bps`;
}

export function compact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toFixed(abs < 10 ? 2 : 0);
}

export function ratioPct(numerator: number, denominator: number, digits = 1): string {
  if (!denominator) return "0.0%";
  return `${((numerator / denominator) * 100).toFixed(digits)}%`;
}

/* --------------------------------- time --------------------------------- */

function toMillis(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return value > 1e15 ? Math.round(value / 1_000_000) : value;
}

export function utc(value: unknown): string {
  if (typeof value !== "number" && typeof value !== "string") return "--:--:--";
  const millis = typeof value === "number" ? toMillis(value) : Date.parse(value);
  if (!Number.isFinite(millis) || millis <= 0) return "--:--:--";
  return new Date(millis).toISOString().replace("T", " ").slice(0, 19);
}

export function clock(millis: number): string {
  if (!Number.isFinite(millis) || millis <= 0) return "--:--";
  const total = Math.max(0, Math.floor(millis / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function shortHash(value: string, head = 8, tail = 4): string {
  if (value.length <= head + tail + 1) return value || "-";
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}

export function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

/* ------------------------------- semantics ------------------------------ */

export function toneOf(value: number): Tone {
  if (value > 0) return "bull";
  if (value < 0) return "bear";
  return "neutral";
}

export function statusTone(status: string): Tone {
  const clean = status.trim().toUpperCase();
  if (["PASS", "OK", "LIVE", "ACTIVE", "READY", "GREEN", "TRUE", "ARMED"].includes(clean)) {
    return "bull";
  }
  if (["FAIL", "DOWN", "HALTED", "BLOCKED", "ERROR", "RED", "FROZEN"].includes(clean)) {
    return "bear";
  }
  if (["WARN", "DEGRADED", "PENDING", "REVIEW", "AMBER"].includes(clean)) return "accent";
  return "muted";
}

export function yesNo(value: boolean): string {
  return value ? "YES" : "NO";
}

export function orDash(value: string): string {
  return value.trim() === "" ? "-" : value;
}
