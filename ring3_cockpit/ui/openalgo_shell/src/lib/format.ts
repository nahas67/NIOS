/**
 * Strict, allocation-light formatters for the glass cockpit.
 *
 * Every helper accepts `unknown` because the gateway payloads are hydrated
 * from telemetry that may legitimately be `null` before the first tick.
 * Nothing here rounds money through a float: micro-unit amounts are divided
 * exactly once at the display boundary.
 */

const EMPTY = "—";

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function toNumber(value: unknown): number | null {
  if (isFiniteNumber(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function num(value: unknown, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  return parsed.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function int(value: unknown): string {
  return num(value, 0);
}

export function compact(value: unknown): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  if (Math.abs(parsed) >= 1_000_000_000) return `${num(parsed / 1_000_000_000, 2)}B`;
  if (Math.abs(parsed) >= 1_000_000) return `${num(parsed / 1_000_000, 2)}M`;
  if (Math.abs(parsed) >= 10_000) return `${num(parsed / 1_000, 1)}k`;
  return num(parsed, 0);
}

export function signed(value: unknown, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${num(parsed, digits)}`;
}

export function price(value: unknown): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  const abs = Math.abs(parsed);
  if (abs >= 10_000) return num(parsed, 1);
  if (abs >= 100) return num(parsed, 2);
  if (abs >= 1) return num(parsed, 3);
  if (abs >= 0.01) return num(parsed, 5);
  return num(parsed, 7);
}

export function bps(value: unknown, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  return `${signed(parsed, digits)} bps`;
}

export function pct(value: unknown, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  return `${num(parsed, digits)}%`;
}

export function ratioPct(value: unknown, digits = 1): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  return `${num(parsed * 100, digits)}%`;
}

/** 1 unit = 1,000,000 micros (IBOR 128-bit integer convention). */
export function fromMicros(micros: unknown): number | null {
  const parsed = toNumber(micros);
  return parsed === null ? null : parsed / 1_000_000;
}

export function money(micros: unknown, currency = "$", digits = 2): string {
  const units = fromMicros(micros);
  if (units === null) return EMPTY;
  const abs = Math.abs(units);
  const formatted = abs >= 1_000_000 ? compact(units) : num(units, digits);
  return `${currency}${formatted}`;
}

export function usd(value: unknown, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return EMPTY;
  return `$${num(parsed, digits)}`;
}

export function hash(value: unknown, head = 10, tail = 6): string {
  if (typeof value !== "string" || value.length === 0) return EMPTY;
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function latency(ms: unknown): string {
  const parsed = toNumber(ms);
  if (parsed === null) return EMPTY;
  if (parsed < 1) return `${num(parsed * 1000, 0)} µs`;
  if (parsed < 1000) return `${num(parsed, 2)} ms`;
  return `${num(parsed / 1000, 2)} s`;
}

export function microLatency(us: unknown): string {
  const parsed = toNumber(us);
  if (parsed === null) return EMPTY;
  if (parsed < 1000) return `${num(parsed, 3)} µs`;
  return `${num(parsed / 1000, 3)} ms`;
}

export function duration(seconds: unknown): string {
  const parsed = toNumber(seconds);
  if (parsed === null) return EMPTY;
  const total = Math.max(0, Math.floor(parsed));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function clock(ns: unknown): string {
  const parsed = toNumber(ns);
  if (parsed === null || parsed <= 0) return EMPTY;
  const date = new Date(parsed / 1_000_000);
  return date.toISOString().slice(11, 19);
}

export function utc(ns: unknown): string {
  const parsed = toNumber(ns);
  if (parsed === null || parsed <= 0) return EMPTY;
  const date = new Date(parsed / 1_000_000);
  return date.toISOString().replace("T", " ").slice(0, 19);
}

export function ago(ns: unknown, nowMs = Date.now()): string {
  const parsed = toNumber(ns);
  if (parsed === null || parsed <= 0) return EMPTY;
  const seconds = Math.max(0, Math.round((nowMs - parsed / 1_000_000) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

export function titleize(key: unknown): string {
  if (typeof key !== "string" || key.length === 0) return EMPTY;
  return key
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function shortId(value: unknown, head = 8): string {
  if (typeof value !== "string" || value.length === 0) return EMPTY;
  return value.length <= head ? value : value.slice(0, head);
}

export function text(value: unknown, fallback = EMPTY): string {
  if (typeof value === "string" && value.trim() !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "YES" : "NO";
  return fallback;
}

export function bool(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return value.toLowerCase() === "true";
  return Boolean(value);
}

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function rows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => row !== null && typeof row === "object")
    : [];
}

export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

export function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter(isFiniteNumber) : [];
}

export function tone(value: number | null): "pos" | "neg" | "dim" {
  if (value === null || value === 0) return "dim";
  return value > 0 ? "pos" : "neg";
}

export function toneOfLabel(value: string): "ok" | "warn" | "bad" | "neutral" {
  const normalized = value.toUpperCase();
  if (["OK", "PASS", "PASSED", "READY", "ACTIVE", "ARMED", "VERIFIED", "LIVE", "UP", "HEALTHY", "COMMITTED", "GRADUATED", "CONFIGURED"].includes(normalized)) return "ok";
  if (["WARN", "WARNING", "DEGRADED", "PENDING", "RECOMMENDED", "HOLD", "PAUSED", "UNSET"].includes(normalized)) return "warn";
  if (["FAIL", "FAILED", "ERROR", "HALTED", "FROZEN", "REJECTED", "VOIDED", "DOWN", "CRITICAL", "EXPIRED"].includes(normalized)) return "bad";
  return "neutral";
}
