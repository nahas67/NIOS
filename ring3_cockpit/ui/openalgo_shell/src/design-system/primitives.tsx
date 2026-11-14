/**
 * V1.4 primitive kit.
 *
 * Ten composable surfaces shared by all fifteen pages and the auth portals.
 * Nothing here knows about a specific endpoint: primitives render evidence,
 * never fetch it. Every interactive control is keyboard reachable and carries
 * an explicit ARIA label.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import {
  ArrowRight,
  Check,
  ChevronRight,
  Copy,
  Search,
  ShieldAlert,
  TerminalSquare,
  X,
} from "lucide-react";
import { PAGES, PAGE_BY_KEY, type PageKey } from "@/pages/registry";
import { bps, hash, int, money, num, pct, price, signed, text } from "@/lib/format";

/* ------------------------------- panels ------------------------------- */

export interface GlassPanelProps {
  title?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
  bodyClassName?: string;
  as?: "section" | "div" | "article";
}

export function GlassPanel({
  title,
  eyebrow,
  actions,
  children,
  footer,
  className = "",
  bodyClassName = "panel-body",
  as: Tag = "section",
}: GlassPanelProps): ReactNode {
  return (
    <Tag className={`glass ${className}`.trim()}>
      {title || actions ? (
        <header className="panel-head">
          <div className="min-w-0">
            {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
            {title ? <h2 className="truncate text-[13px] font-semibold text-[var(--ink)]">{title}</h2> : null}
          </div>
          {actions ? <div className="flex flex-none items-center gap-1.5">{actions}</div> : null}
        </header>
      ) : null}
      <div className={bodyClassName}>{children}</div>
      {footer ? <div className="border-t border-[var(--line)] px-4 py-2.5">{footer}</div> : null}
    </Tag>
  );
}

export interface SpecularCardProps {
  title: ReactNode;
  subtitle?: ReactNode;
  value?: ReactNode;
  footer?: ReactNode;
  accent?: string;
  children?: ReactNode;
  className?: string;
}

export function SpecularCard({
  title,
  subtitle,
  value,
  footer,
  accent,
  children,
  className = "",
}: SpecularCardProps): ReactNode {
  return (
    <div
      className={`specular p-4 ${className}`.trim()}
      style={accent ? ({ ["--accent" as string]: accent } as CSSProperties) : undefined}
    >
      <p className="eyebrow">{title}</p>
      {subtitle ? <p className="mt-0.5 text-[11.5px] text-[var(--muted)]">{subtitle}</p> : null}
      {value ? <div className="mono tnum mt-2 text-[22px] font-semibold tracking-tight">{value}</div> : null}
      {children}
      {footer ? <div className="mt-3">{footer}</div> : null}
    </div>
  );
}

/* ------------------------------- metrics ------------------------------- */

export type ValueKind = "num" | "int" | "money" | "price" | "bps" | "pct" | "hash" | "text" | "usd";

export interface TabularValueProps {
  value: unknown;
  kind?: ValueKind;
  digits?: number;
  currency?: string;
  className?: string;
  signTone?: boolean;
}

export function TabularValue({
  value,
  kind = "num",
  digits = 2,
  currency = "$",
  className = "",
  signTone = false,
}: TabularValueProps): ReactNode {
  let rendered: string;
  switch (kind) {
    case "int":
      rendered = int(value);
      break;
    case "money":
      rendered = money(value, currency, digits);
      break;
    case "price":
      rendered = price(value);
      break;
    case "bps":
      rendered = bps(value, digits);
      break;
    case "pct":
      rendered = pct(value, digits);
      break;
    case "hash":
      rendered = hash(value);
      break;
    case "usd":
      rendered = `$${num(value, digits)}`;
      break;
    case "text":
      rendered = text(value);
      break;
    default:
      rendered = num(value, digits);
  }
  const numeric = typeof value === "number" ? value : Number(value);
  const tone =
    signTone && Number.isFinite(numeric) && numeric !== 0
      ? numeric > 0
        ? "pos"
        : "neg"
      : "";
  return <span className={`tnum ${tone} ${className}`.trim()}>{rendered}</span>;
}

export interface MetricTileProps {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "pos" | "neg" | "warn" | "dim" | "accent";
  mono?: boolean;
  className?: string;
}

export function MetricTile({ label, value, hint, tone, mono = true, className = "" }: MetricTileProps): ReactNode {
  return (
    <div className={`tile ${className}`.trim()}>
      <p className="kpi-label">{label}</p>
      <div className={`kpi-value ${tone ? tone : ""} ${mono ? "mono tnum" : ""}`.trim()}>{value}</div>
      {hint ? <p className="kpi-hint">{hint}</p> : null}
    </div>
  );
}

/* ------------------------------- pills ------------------------------- */

export type PillTone = "ok" | "warn" | "bad" | "info" | "accent" | "accent2" | "neutral";

export interface StatusPillProps {
  value: unknown;
  tone?: PillTone;
  dot?: boolean;
  title?: string;
  className?: string;
}

const TONE_CLASS: Record<PillTone, string> = {
  ok: "pill-ok",
  warn: "pill-warn",
  bad: "pill-bad",
  info: "pill-info",
  accent: "pill-accent",
  accent2: "pill-accent2",
  neutral: "",
};

export function StatusPill({ value, tone = "neutral", dot = true, title, className = "" }: StatusPillProps): ReactNode {
  const label = text(value, "—");
  return (
    <span className={`pill ${TONE_CLASS[tone]} ${className}`.trim()} title={title ?? label}>
      {dot ? <span className="pill-dot" aria-hidden="true" /> : null}
      {label}
    </span>
  );
}

export function passTone(flag: boolean | null | undefined): PillTone {
  if (flag === true) return "ok";
  if (flag === false) return "bad";
  return "neutral";
}

/* ------------------------------- headers ------------------------------- */

export interface SectionHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

export function SectionHeader({ title, subtitle, eyebrow, actions, className = "" }: SectionHeaderProps): ReactNode {
  return (
    <div className={`page-head ${className}`.trim()}>
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h2 className="page-title">{title}</h2>
        {subtitle ? <p className="page-sub mt-0.5">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </div>
  );
}

/* ------------------------------- empty / error ------------------------------- */

export interface EmptyCleanSlateProps {
  title: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
}

export function EmptyCleanSlate({ title, body, action }: EmptyCleanSlateProps): ReactNode {
  return (
    <div className="empty">
      <div className="empty-ring" aria-hidden="true">
        <TerminalSquare size={17} />
      </div>
      <p className="text-[12.5px] font-medium text-[var(--ink-soft)]">{title}</p>
      {body ? <p className="max-w-[46ch] text-[11.5px] text-[var(--dim)]">{body}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock({ rows = 3, label = "Hydrating telemetry" }: { rows?: number; label?: string }): ReactNode {
  return (
    <div className="flex flex-col gap-2" role="status" aria-label={label}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="shimmer h-4" style={{ width: `${100 - index * 9}%` }} />
      ))}
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }): ReactNode {
  return (
    <div className="flex flex-col items-start gap-2 rounded-[var(--r-md)] border border-[rgba(251,113,133,0.35)] bg-[var(--neg-soft)] p-3">
      <div className="row">
        <ShieldAlert size={14} className="text-[var(--neg)]" aria-hidden="true" />
        <p className="text-[12px] text-[var(--neg)]">Telemetry unavailable</p>
      </div>
      <p className="mono text-[10.5px] break-words text-[var(--ink-soft)]">{message}</p>
      {onRetry ? (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

/* ------------------------------- progress ------------------------------- */

export function InlineBar({
  value,
  max = 100,
  tone = "accent",
  label,
}: {
  value: number;
  max?: number;
  tone?: "accent" | "pos" | "warn" | "neg";
  label?: string;
}): ReactNode {
  const safeMax = max <= 0 ? 1 : max;
  const width = Math.max(0, Math.min(100, (value / safeMax) * 100));
  const color =
    tone === "pos" ? "var(--pos)" : tone === "warn" ? "var(--warn)" : tone === "neg" ? "var(--neg)" : "var(--accent)";
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-[rgba(148,163,184,0.14)]"
      role="progressbar"
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={Math.round(safeMax)}
      aria-label={label ?? "progress"}
    >
      <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${width}%`, background: color }} />
    </div>
  );
}

/* ------------------------------- tabs ------------------------------- */

export function TabStrip<T extends string>({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: Array<{ id: T; label: string; count?: number }>;
  active: T;
  onChange: (id: T) => void;
  label: string;
}): ReactNode {
  return (
    <div className="seg" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={active === tab.id}
          className="seg-item"
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          {tab.count !== undefined ? <span className="ml-1.5 text-[var(--dim)]">{tab.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------- copy hash ------------------------------- */

export function CopyHash({ value, label = "copy digest" }: { value: unknown; label?: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  const digest = typeof value === "string" ? value : "";
  const copy = useCallback(() => {
    if (!digest) return;
    void navigator.clipboard
      ?.writeText(digest)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1600);
      })
      .catch(() => setCopied(false));
  }, [digest]);

  if (!digest) return <span className="mono text-[10.5px] text-[var(--dim)]">—</span>;
  return (
    <button
      type="button"
      className="row gap-1.5 text-left"
      onClick={copy}
      title={digest}
      aria-label={`${label}: ${digest}`}
    >
      <span className="mono tnum text-[10.5px] text-[var(--accent)]">{hash(digest)}</span>
      {copied ? (
        <Check size={11} className="text-[var(--pos)]" aria-hidden="true" />
      ) : (
        <Copy size={11} className="text-[var(--dim)]" aria-hidden="true" />
      )}
    </button>
  );
}

/* ------------------------------- danger modal ------------------------------- */

export interface ConfirmDangerModalProps {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  requireText?: string;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDangerModal({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  requireText,
  busy = false,
  onCancel,
  onConfirm,
}: ConfirmDangerModalProps): ReactNode {
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setTyped("");
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    inputRef.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  const ready = !requireText || typed.trim().toUpperCase() === requireText.toUpperCase();

  return (
    <div className="overlay overlay-center">
      <button
        type="button"
        className="overlay-backdrop"
        aria-label="Close dialog"
        onClick={onCancel}
      />
      <div
        className="modal modal-danger"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex items-start gap-3 border-b border-[var(--line)] p-4">
          <span className="mt-0.5 text-[var(--neg)]" aria-hidden="true">
            <ShieldAlert size={18} />
          </span>
          <div className="min-w-0">
            <h2 className="text-[14px] font-semibold">{title}</h2>
            <div className="mt-1 text-[12px] text-[var(--muted)]">{body}</div>
          </div>
          <button type="button" className="btn btn-ghost btn-icon ml-auto" onClick={onCancel} aria-label="Close dialog">
            <X size={13} />
          </button>
        </div>
        <div className="p-4">
          {requireText ? (
            <label className="field">
              <span className="field-label">Type {requireText} to confirm</span>
              <input
                ref={inputRef}
                className="input tnum"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                aria-label={`Type ${requireText} to confirm`}
                autoComplete="off"
              />
            </label>
          ) : null}
          <div className="mt-4 flex justify-end gap-2">
            <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" onClick={onConfirm} disabled={!ready || busy}>
              {busy ? "Executing…" : confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------- receipt drawer ------------------------------- */

export function ReceiptDrawer({
  title,
  open,
  receipt,
  error,
  onClose,
}: {
  title: string;
  open: boolean;
  receipt: unknown;
  error?: string | null;
  onClose: () => void;
}): ReactNode {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <aside className="drawer" role="dialog" aria-modal="true" aria-label={`${title} receipt`}>
      <header className="flex items-center justify-between border-b border-[var(--line)] px-4 py-3">
        <div>
          <p className="eyebrow">Proof receipt</p>
          <h2 className="text-[13px] font-semibold">{title}</h2>
        </div>
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close receipt">
          <X size={14} />
        </button>
      </header>
      <div className="scroll-y flex-1 p-4">
        {error ? <ErrorBlock message={error} /> : null}
        <pre className="code-block">{JSON.stringify(receipt ?? { status: "no receipt yet" }, null, 2)}</pre>
      </div>
    </aside>
  );
}

/* ------------------------------- command palette ------------------------------- */

const PALETTE_EVENT = "nios:palette";

export function openPalette(): void {
  window.dispatchEvent(new Event(PALETTE_EVENT));
}

export function CommandPalette({ onNavigate }: { onNavigate: (page: PageKey) => void }): ReactNode {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return PAGES;
    return PAGES.filter((page) =>
      [page.title, page.short, page.summary, page.key, page.ring]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [query]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(PALETTE_EVENT, onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(PALETTE_EVENT, onOpen);
    };
  }, []);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setCursor(0);
      return;
    }
    inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const choose = (page: PageKey) => {
    setOpen(false);
    onNavigate(page);
  };

  return (
    <div className="overlay">
      <button
        type="button"
        className="overlay-backdrop"
        aria-label="Close command palette"
        onClick={() => setOpen(false)}
      />
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Quick jump command palette"
      >
        <div className="flex items-center gap-2 border-b border-[var(--line)] px-4">
          <Search size={14} className="text-[var(--dim)]" aria-hidden="true" />
          <input
            ref={inputRef}
            className="palette-input"
            placeholder="Jump to a workstation — dashboard, terminal, ledger…"
            value={query}
            aria-controls={listId}
            aria-label="Search pages"
            onChange={(event) => {
              setQuery(event.target.value);
              setCursor(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false);
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setCursor((value) => Math.min(results.length - 1, value + 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setCursor((value) => Math.max(0, value - 1));
              }
              if (event.key === "Enter" && results[cursor]) {
                event.preventDefault();
                choose(results[cursor].key);
              }
            }}
          />
          <span className="kbd">ESC</span>
        </div>
        <div className="palette-list" id={listId} role="listbox" aria-label="Pages">
          {results.length === 0 ? (
            <p className="px-3 py-6 text-center text-[12px] text-[var(--dim)]">No workstation matches that query.</p>
          ) : (
            results.map((page, index) => (
              <button
                key={page.key}
                type="button"
                role="option"
                aria-selected={index === cursor}
                data-active={index === cursor}
                className="palette-item"
                onMouseEnter={() => setCursor(index)}
                onClick={() => choose(page.key)}
              >
                <span className="mono text-[9.5px] text-[var(--dim)]">{page.ring}</span>
                <span className="flex-1 truncate">{page.title}</span>
                <span className="mono text-[9.5px] text-[var(--dim)]">{page.route}</span>
                <ChevronRight size={12} className="text-[var(--dim)]" aria-hidden="true" />
              </button>
            ))
          )}
        </div>
        <footer className="flex items-center justify-between border-t border-[var(--line)] px-4 py-2">
          <span className="mono text-[9.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
            15 production pages · <span className="text-[var(--accent)]">Clean slate</span> ready
          </span>
          <span className="mono text-[9.5px] text-[var(--dim)]">↑ ↓ navigate · ⏎ open</span>
        </footer>
      </div>
    </div>
  );
}

/* ------------------------------- misc ------------------------------- */

export function PageLink({
  page,
  children,
  onNavigate,
}: {
  page: PageKey;
  children?: ReactNode;
  onNavigate: (page: PageKey) => void;
}): ReactNode {
  const meta = PAGE_BY_KEY[page];
  return (
    <button type="button" className="btn btn-sm btn-ghost" onClick={() => onNavigate(page)}>
      {children ?? meta.short}
      <ArrowRight size={11} aria-hidden="true" />
    </button>
  );
}

export function Freshness({
  generatedAtNs,
  onRefresh,
  busy,
}: {
  generatedAtNs: unknown;
  onRefresh?: () => void;
  busy?: boolean;
}): ReactNode {
  const ns = typeof generatedAtNs === "number" ? generatedAtNs : null;
  const stamp = ns ? new Date(ns / 1_000_000).toISOString().slice(11, 19) : "—";
  return (
    <div className="row gap-2">
      <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">snapshot {stamp} UTC</span>
      {onRefresh ? (
        <button type="button" className="btn btn-sm btn-ghost" onClick={onRefresh} disabled={busy}>
          Refresh
        </button>
      ) : null}
    </div>
  );
}

export function toneForNumber(value: number | null): "pos" | "neg" | "dim" {
  if (value === null || value === 0) return "dim";
  return value > 0 ? "pos" : "neg";
}

/** Maps a signed number onto the pill palette (profit / loss / idle). */
export function pillTone(value: number | null): "pos" | "neg" | "dim" {
  return toneForNumber(value);
}

export function formatSignedBps(value: unknown): ReactNode {
  return <span className="mono tnum">{bps(value)}</span>;
}

export function formatSignedNumber(value: unknown): ReactNode {
  return <span className="mono tnum">{signed(value)}</span>;
}
