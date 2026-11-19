/**
 * NIOS terminal formatting primitives.
 *
 * Every figure that reaches the screen flows through one of these helpers so
 * the whole terminal shares a single numeric dialect: exact integers for
 * money, micro-units in, tabular human units out, and a hard `—` fallback for
 * anything the backend has not produced yet.
 */

export const MICROS = 1_000_000;

export type Tone = "neutral" | "bull" | "bear" | "accent" | "muted";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asRows(value: unknown): Record<string, unknown>[] {
  return asArray(value).filter(isRecord);
}

export function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string") return value.length > 0 ? value : fallback;
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : fallback;
  if (typeof value === "boolean") return value ? "true" : "false";
  return fallback;
}

export function safeNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : fallback;
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  return fallback;
}

export function safeBool(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") return value.toLowerCase() === "true";
  return fallback;
}

export function deepGet(source: unknown, path: string): unknown {
  const parts = path.split(".").filter((part) => part.length > 0);
  let cursor: unknown = source;
  for (const part of parts) {
    if (!isRecord(cursor)) return undefined;
    cursor = cursor[part];
  }
  return cursor;
}

export function pick<T>(source: unknown, keys: string[], fallback: T): T {
  for (const key of keys) {
    const value = deepGet(source, key);
    if (value !== undefined && value !== null && value !== "") return value as T;
  }
  return fallback;
}

export function num(value: unknown, digits = 2): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function int(value: unknown): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return Math.trunc(n).toLocaleString("en-US");
}

export function signed(value: unknown, digits = 2): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${num(n, digits)}`;
}

/** Micro-units (1 unit = 1_000_000) to a human decimal string. */
export function units(micros: unknown, digits = 2): string {
  const n = safeNumber(micros, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return num(n / MICROS, digits);
}

export function unitsSigned(micros: unknown, digits = 2): string {
  const n = safeNumber(micros, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return signed(n / MICROS, digits);
}

export function usd(micros: unknown, digits = 2): string {
  const body = units(micros, digits);
  return body === "—" ? "—" : `$${body}`;
}

export function usdSigned(micros: unknown, digits = 2): string {
  const body = unitsSigned(micros, digits);
  return body === "—" ? "—" : `$${body}`;
}

export function pct(value: unknown, digits = 2): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return `${num(n, digits)}%`;
}

export function bps(value: unknown, digits = 1): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return `${num(n, digits)} bps`;
}

export function priceMicros(value: unknown, digits = 2): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return num(n / MICROS, digits);
}

export function qty(value: unknown, digits = 0): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return num(n, digits);
}

export function compact(value: unknown): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function duration(seconds: unknown): string {
  const n = Math.max(0, Math.trunc(safeNumber(seconds, 0)));
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  const pad = (v: number) => String(v).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function timeUtc(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(11, 19);
}

export function dateUtc(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(0, 10);
}

/** Nanosecond epoch → `HH:MM:SS` UTC. */
export function clockNs(value: unknown): string {
  const n = safeNumber(value, Number.NaN);
  if (!Number.isFinite(n) || n <= 0) return "—";
  return new Date(n / 1e6).toISOString().slice(11, 19);
}

export function shortHash(value: unknown, length = 10): string {
  const s = text(value, "");
  if (s.length === 0) return "—";
  return s.length <= length ? s : `${s.slice(0, length)}…`;
}

export function toneForNumber(value: unknown): "bull" | "bear" | "muted" {
  const n = safeNumber(value, 0);
  if (n > 0) return "bull";
  if (n < 0) return "bear";
  return "muted";
}

export function toneClass(tone: Tone): string {
  switch (tone) {
    case "bull":
      return "text-bull";
    case "bear":
      return "text-bear";
    case "accent":
      return "text-accent";
    case "muted":
      return "text-ink-3";
    default:
      return "text-ink";
  }
}

export function signedToneClass(value: unknown): string {
  const n = safeNumber(value, 0);
  if (n > 0) return "text-bull";
  if (n < 0) return "text-bear";
  return "text-ink-2";
}

export function upper(value: unknown, fallback = "—"): string {
  const s = text(value, fallback);
  return s.toUpperCase();
}

export function titleize(value: unknown): string {
  const s = text(value, "");
  if (s.length === 0) return "—";
  return s
    .toLowerCase()
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function namespaceLabel(value: unknown): string {
  return text(value, "—").replaceAll("_", " ");
}

export function nowNs(): number {
  return Date.now() * 1e6;
}
