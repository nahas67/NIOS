/**
 * The V1.5 terminal primitive set.
 *
 * Every surface in the cockpit is composed from these twelve components. They
 * are deliberately austere: one hairline, one of three charcoal tiers, one
 * accent. There is no elevation prop, no translucent variant and no rounded
 * pill shape anywhere in this module.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { PAGES, PAGE_BY_KEY, type PageKey } from "@/pages/registry";
import type { Tone } from "@/lib/types";
import { number, signed } from "@/lib/format";

/* -------------------------------- buttons -------------------------------- */

type BtnTone = "default" | "primary" | "ghost" | "danger";

export interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: BtnTone;
  size?: "sm" | "md";
  block?: boolean;
  armed?: boolean;
}

export function Btn({
  tone = "default",
  size = "md",
  block = false,
  armed = false,
  className = "",
  children,
  type = "button",
  ...rest
}: BtnProps): ReactNode {
  const classes = [
    "nx-btn",
    tone === "primary" ? "nx-btn--primary" : "",
    tone === "ghost" ? "nx-btn--ghost" : "",
    tone === "danger" ? (armed ? "nx-btn--danger-armed" : "nx-btn--danger") : "",
    size === "sm" ? "nx-btn--sm" : "",
    block ? "nx-btn--block" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button type={type} className={classes} {...rest}>
      {children}
    </button>
  );
}

/* -------------------------------- section -------------------------------- */

export interface SectionHeaderProps {
  kicker: string;
  title: string;
  sub?: string;
  actions?: ReactNode;
}

export function SectionHeader({ kicker, title, sub, actions }: SectionHeaderProps): ReactNode {
  return (
    <header className="nx-section">
      <div className="nx-section-id">
        <span className="nx-kicker">{kicker}</span>
        <h1>{title}</h1>
        {sub ? <p className="nx-section-sub">{sub}</p> : null}
      </div>
      {actions ? <div className="nx-section-actions">{actions}</div> : null}
    </header>
  );
}

/* --------------------------------- panel --------------------------------- */

export interface TerminalPanelProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  tone?: "default" | "accent" | "bull" | "bear";
  flush?: boolean;
  bodyClass?: string;
}

export function TerminalPanel({
  title,
  subtitle,
  actions,
  footer,
  children,
  tone = "default",
  flush = false,
  bodyClass = "",
}: TerminalPanelProps): ReactNode {
  const classes = [
    "nx-panel",
    tone === "accent" ? "nx-panel--accent" : "",
    tone === "bull" ? "nx-panel--bull" : "",
    tone === "bear" ? "nx-panel--bear" : "",
    flush ? "nx-panel--flush" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <section className={classes} aria-label={title}>
      <div className="nx-panel-head">
        <div className="nx-panel-title">
          <strong>{title}</strong>
          {subtitle ? <span>{subtitle}</span> : null}
        </div>
        {actions ? <div className="nx-panel-actions">{actions}</div> : null}
      </div>
      <div className={["nx-panel-body", flush ? "nx-panel-body--tight" : "", bodyClass].filter(Boolean).join(" ")}>
        {children}
      </div>
      {footer ? <div className="nx-panel-foot">{footer}</div> : null}
    </section>
  );
}

/* --------------------------------- badges -------------------------------- */

interface ToneShape {
  className: string;
}

const TONE_CLASS: Record<Tone, ToneShape> = {
  bull: { className: "nx-badge--bull" },
  bear: { className: "nx-badge--bear" },
  accent: { className: "nx-badge--accent" },
  neutral: { className: "" },
  muted: { className: "nx-badge--muted" },
};

export interface StatusBadgeProps {
  tone?: Tone;
  dot?: boolean;
  large?: boolean;
  title?: string;
  children: ReactNode;
}

export function StatusBadge({
  tone = "muted",
  dot = false,
  large = false,
  title,
  children,
}: StatusBadgeProps): ReactNode {
  const classes = ["nx-badge", TONE_CLASS[tone].className, large ? "nx-badge--lg" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={classes} title={title}>
      {dot ? <i className="nx-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

export interface DeltaBadgeProps {
  /** Signed value already expressed in `unit` (percent by default). */
  value: number;
  unit?: "%" | "bps" | "";
  digits?: number;
  /** Force the semantic colour instead of deriving it from the sign. */
  tone?: Tone;
  label?: string;
}

export function DeltaBadge({
  value,
  unit = "%",
  digits = 2,
  tone,
  label,
}: DeltaBadgeProps): ReactNode {
  const resolved: Tone = tone ?? (value > 0 ? "bull" : value < 0 ? "bear" : "neutral");
  const text = unit === "" ? signed(value, digits) : `${signed(value, digits)} ${unit}`;
  return (
    <StatusBadge tone={resolved} title={label ?? "change"}>
      {text}
    </StatusBadge>
  );
}

/* --------------------------------- tiles --------------------------------- */

export interface MetricTileProps {
  label: string;
  value: string;
  delta?: number;
  deltaUnit?: "%" | "bps" | "";
  sub?: string;
  footnote?: string;
  tone?: Tone;
  badge?: ReactNode;
  raised?: boolean;
}

export function MetricTile({
  label,
  value,
  delta,
  deltaUnit = "%",
  sub,
  footnote,
  tone = "neutral",
  badge,
  raised = false,
}: MetricTileProps): ReactNode {
  const valueClass =
    tone === "bull" ? "nx-bull" : tone === "bear" ? "nx-bear" : tone === "accent" ? "nx-accent" : "";
  return (
    <div className={raised ? "nx-tile nx-tile--raised" : "nx-tile"}>
      <div className="nx-tile-head">
        <span className="nx-tile-label">{label}</span>
        {badge ?? null}
      </div>
      <span className={`nx-tile-value ${valueClass}`}>{value}</span>
      {delta !== undefined ? (
        <div className="nx-tile-foot">
          <DeltaBadge value={delta} unit={deltaUnit} />
          {sub ? <span className="nx-truncate">{sub}</span> : null}
        </div>
      ) : sub ? (
        <div className="nx-tile-foot">
          <span className="nx-truncate">{sub}</span>
        </div>
      ) : null}
      {footnote ? <span className="nx-xs nx-muted nx-truncate">{footnote}</span> : null}
    </div>
  );
}

/* ---------------------------------- meter -------------------------------- */

export interface MeterProps {
  value: number;
  max?: number;
  tone?: "accent" | "bull" | "bear";
  label?: string;
}

export function Meter({ value, max = 100, tone = "accent", label }: MeterProps): ReactNode {
  const safeMax = max > 0 ? max : 1;
  const pctWidth = Math.max(0, Math.min(100, (value / safeMax) * 100));
  return (
    // biome-ignore lint/a11y/useSemanticElements: the level bar is a styled div; a native meter cannot carry the track and fill split
    <div
      className="nx-meter"
      role="meter"
      aria-label={label ?? "level"}
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={Math.round(max)}
    >
      <div
        className={`nx-meter-fill${tone === "bull" ? " nx-meter-fill--bull" : tone === "bear" ? " nx-meter-fill--bear" : ""}`}
        style={{ width: `${pctWidth}%` }}
      />
    </div>
  );
}

/* ------------------------------- empty state ----------------------------- */

export interface EmptyCleanSlateProps {
  title: string;
  body?: string;
  hint?: string;
  action?: ReactNode;
}

export function EmptyCleanSlate({ title, body, hint, action }: EmptyCleanSlateProps): ReactNode {
  return (
    <div className="nx-empty">
      <strong>{title}</strong>
      {body ? <p>{body}</p> : null}
      {hint ? <span className="nx-empty-hint">{hint}</span> : null}
      {action}
    </div>
  );
}

/* --------------------------------- fields -------------------------------- */

export interface FieldProps {
  label: string;
  hint?: string;
  children: ReactNode;
}

export function Field({ label, hint, children }: FieldProps): ReactNode {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is supplied as children by every call site
    <label className="nx-field">
      <span className="nx-label">{label}</span>
      {children}
      {hint ? <span className="nx-xs nx-muted">{hint}</span> : null}
    </label>
  );
}

/* -------------------------------- data grid ------------------------------ */

export interface Column<T> {
  key: string;
  label: string;
  align?: "left" | "right";
  render: (row: T, index: number) => ReactNode;
}

export interface DataGridProps<T> {
  columns: Column<T>[];
  rows: T[];
  getKey?: (row: T, index: number) => string;
  empty?: ReactNode;
  caption?: string;
}

export function DataGrid<T>({ columns, rows, getKey, empty, caption }: DataGridProps<T>): ReactNode {
  if (rows.length === 0) {
    return <>{empty ?? <EmptyCleanSlate title="No rows" body="Nothing is recorded for this query yet." />}</>;
  }
  return (
    <div className="nx-table-wrap">
      <table className="nx-table">
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} scope="col" style={{ textAlign: column.align ?? "left" }}>
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={getKey ? getKey(row, index) : String(index)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={column.align === "right" ? "nx-td-num" : undefined}
                >
                  {column.render(row, index)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* --------------------------------- modal --------------------------------- */

export interface ConfirmModalProps {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  children?: ReactNode;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "default",
  busy = false,
  children,
  onConfirm,
  onClose,
}: ConfirmModalProps): ReactNode {
  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="nx-overlay" role="presentation">
      <dialog className="nx-modal" open aria-modal="true" aria-label={title}>
        <div className="nx-modal-head">
          <div>
            <h2>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button type="button" className="nx-modal-x" onClick={onClose} aria-label="Close dialog">
            x
          </button>
        </div>
        {children ? <div className="nx-modal-body">{children}</div> : null}
        <div className="nx-modal-foot">
          <Btn tone="ghost" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </Btn>
          <Btn tone={tone === "danger" ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "Working..." : confirmLabel}
          </Btn>
        </div>
      </dialog>
    </div>
  );
}

/* ----------------------------- command palette --------------------------- */

const PALETTE_EVENT = "nios:palette";

export function openPalette(): void {
  window.dispatchEvent(new Event(PALETTE_EVENT));
}

export interface CommandPaletteProps {
  onNavigate: (page: PageKey) => void;
}

export function CommandPalette({ onNavigate }: CommandPaletteProps): ReactNode {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const matches = useMemo(() => {
    const clean = query.trim().toLowerCase();
    if (clean === "") return PAGES;
    return PAGES.filter((page) => {
      const haystack = `${page.title} ${page.short} ${page.key} ${page.summary}`.toLowerCase();
      return haystack.includes(clean);
    });
  }, [query]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setCursor(0);
  }, []);

  useEffect(() => {
    const onPalette = () => {
      setOpen((value) => !value);
      setQuery("");
      setCursor(0);
    };
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onPalette();
      }
    };
    window.addEventListener(PALETTE_EVENT, onPalette);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener(PALETTE_EVENT, onPalette);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const select = (index: number) => {
    const page = matches[index];
    if (!page) return;
    onNavigate(page.key);
    close();
  };

  return (
    <div className="nx-palette-overlay" role="presentation">
      <div className="nx-palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="nx-palette-head">
          <span className="nx-kicker">GO</span>
          <input
            ref={inputRef}
            className="nx-palette-input"
            placeholder="Jump to a workstation, portfolio or proof surface..."
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setCursor(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                close();
              }
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setCursor((value) => Math.min(value + 1, matches.length - 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setCursor((value) => Math.max(value - 1, 0));
              }
              if (event.key === "Enter") {
                event.preventDefault();
                select(cursor);
              }
            }}
            aria-label="Search workstations"
          />
          <span className="nx-kbd">ESC</span>
        </div>
        <div className="nx-palette-list">
          {matches.length === 0 ? (
            <div className="nx-palette-group">no workstation matches</div>
          ) : (
            matches.map((page, index) => (
              <button
                key={page.key}
                type="button"
                className={`nx-palette-item${index === cursor ? " nx-palette-item--active" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={() => select(index)}
              >
                <span className="nx-mono nx-muted" style={{ width: 58 }}>
                  {PAGE_BY_KEY[page.key].ring}
                </span>
                <span className="nx-truncate" style={{ flex: "1 1 auto" }}>
                  {page.title}
                </span>
                <span className="nx-xs nx-muted">{page.route}</span>
              </button>
            ))
          )}
        </div>
        <div className="nx-palette-foot">
          <span className="nx-kbd">CTRL/CMD</span>
          <span className="nx-kbd">K</span>
          <span>15 production pages under one sovereign router</span>
          <span style={{ marginLeft: "auto" }}>{number(matches.length, 0)} results</span>
        </div>
      </div>
    </div>
  );
}
