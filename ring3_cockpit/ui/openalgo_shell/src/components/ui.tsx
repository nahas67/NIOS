/**
 * NIOS V2.2 terminal primitives.
 *
 * One flat vocabulary for the whole terminal: charcoal layers, 1px hairlines,
 * 4/6/8px radii, tabular numerals.  No elevation, no blur, no gradients.
 */

import type { ChangeEvent, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { useEffect } from "react";
import { Icon, type IconName } from "./Icon";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter((part): part is string => typeof part === "string" && part.length > 0).join(" ");
}

export type BadgeTone = "neutral" | "accent" | "bull" | "bear" | "muted" | "warn";

const BADGE_TONE: Record<BadgeTone, string> = {
  neutral: "bg-elevated text-ink-2 border-hairline",
  accent: "bg-elevated text-accent border-hairline",
  bull: "bg-bull-tint text-bull border-bull-border",
  bear: "bg-bear-tint text-bear border-bear-border",
  muted: "bg-canvas text-ink-3 border-hairline",
  warn: "bg-bear-tint text-[#E0A055] border-[#4A3A22]",
};

export function Badge({
  children,
  tone = "neutral",
  className,
  mono,
}: {
  children: ReactNode;
  tone?: BadgeTone;
  className?: string;
  mono?: boolean;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-[4px] border px-1.5 py-[1px] text-[10px] font-semibold uppercase tracking-[0.1em] leading-[16px]",
        mono && "font-mono tracking-[0.04em]",
        BADGE_TONE[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
  dense,
  icon,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  bodyClassName?: string;
  dense?: boolean;
  icon?: IconName;
}) {
  return (
    <section className={cx("flex min-w-0 flex-col rounded-[6px] border border-hairline bg-surface", className)}>
      {title !== undefined ? (
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-hairline px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            {icon ? <Icon name={icon} size={13} className="shrink-0 text-ink-3" /> : null}
            <div className="min-w-0">
              <h2 className="truncate text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-2">{title}</h2>
              {subtitle !== undefined ? <p className="truncate text-[10px] text-ink-3">{subtitle}</p> : null}
            </div>
          </div>
          {actions !== undefined ? <div className="flex shrink-0 items-center gap-1.5">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cx("min-w-0 flex-1", dense ? "p-0" : "p-3", bodyClassName)}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
  size = "md",
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "bull" | "bear" | "accent" | "muted";
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const toneClass =
    tone === "bull" ? "text-bull" : tone === "bear" ? "text-bear" : tone === "accent" ? "text-accent" : tone === "muted" ? "text-ink-3" : "text-ink";
  const sizeClass = size === "lg" ? "text-[22px]" : size === "sm" ? "text-[13px]" : "text-[17px]";
  return (
    <div className={cx("min-w-0", className)}>
      <div className="truncate text-[10px] font-medium uppercase tracking-[0.14em] text-ink-3">{label}</div>
      <div className={cx("mt-1 truncate font-semibold tabular-nums leading-tight", sizeClass, toneClass)}>{value}</div>
      {hint !== undefined ? <div className="mt-0.5 truncate text-[10px] text-ink-3">{hint}</div> : null}
    </div>
  );
}

export function StatGrid({ children, columns = 4, className }: { children: ReactNode; columns?: number; className?: string }) {
  const grid: Record<number, string> = {
    2: "grid-cols-2",
    3: "grid-cols-2 md:grid-cols-3",
    4: "grid-cols-2 xl:grid-cols-4",
    5: "grid-cols-2 md:grid-cols-3 xl:grid-cols-5",
    6: "grid-cols-2 md:grid-cols-3 xl:grid-cols-6",
  };
  return <div className={cx("grid gap-3", grid[columns] ?? grid[4], className)}>{children}</div>;
}

export type ButtonVariant = "primary" | "ghost" | "danger" | "subtle" | "success";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary: "border border-accent bg-accent text-[#0D0E10] hover:bg-[#E85A12] font-semibold",
  ghost: "border border-hairline bg-transparent text-ink-2 hover:border-[#3A3D44] hover:bg-elevated hover:text-ink",
  danger: "border border-bear-border bg-bear-tint text-bear hover:border-[#6A3538] hover:bg-[#3A1F20]",
  subtle: "border border-hairline bg-elevated text-ink-2 hover:text-ink",
  success: "border border-bull-border bg-bull-tint text-bull hover:border-[#2E5A46]",
};

export function Button({
  children,
  onClick,
  variant = "ghost",
  size = "md",
  disabled,
  type = "button",
  className,
  title,
  icon,
  active,
}: {
  children?: ReactNode;
  onClick?: () => void;
  variant?: ButtonVariant;
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
  icon?: IconName;
  active?: boolean;
}) {
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-[4px] tabular-nums transition-colors duration-75 disabled:opacity-40",
        size === "sm" ? "h-6 px-2 text-[10px]" : "h-7 px-2.5 text-[11px]",
        "font-medium uppercase tracking-[0.08em]",
        active ? BUTTON_VARIANT.primary : BUTTON_VARIANT[variant],
        className,
      )}
    >
      {icon ? <Icon name={icon} size={12} /> : null}
      {children}
    </button>
  );
}

export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cx("block min-w-0", className)}>
      <span className="mb-1 block text-[10px] font-medium uppercase tracking-[0.14em] text-ink-3">{label}</span>
      {children}
      {hint !== undefined ? <span className="mt-1 block text-[10px] leading-snug text-ink-3">{hint}</span> : null}
    </label>
  );
}

export function TextInput({ className, mono, ...rest }: InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }) {
  return (
    <input
      {...rest}
      className={cx(
        "h-7 w-full min-w-0 rounded-[4px] border border-hairline bg-canvas px-2 text-[12px] text-ink placeholder:text-ink-3",
        "focus:border-accent focus:outline-none disabled:opacity-50",
        mono && "font-mono text-[11px]",
        className,
      )}
    />
  );
}

export function TextArea({ className, rows = 3, value, onChange, placeholder }: { className?: string; rows?: number; value: string; onChange: (value: string) => void; placeholder?: string }) {
  return (
    <textarea
      rows={rows}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className={cx(
        "w-full min-w-0 resize-y rounded-[4px] border border-hairline bg-canvas px-2 py-1.5 text-[12px] text-ink placeholder:text-ink-3",
        "focus:border-accent focus:outline-none",
        className,
      )}
    />
  );
}

export interface SelectOption {
  value: string;
  label: string;
}

export function SelectInput({
  value,
  onChange,
  options,
  className,
  ...rest
}: Omit<SelectHTMLAttributes<HTMLSelectElement>, "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
}) {
  return (
    <select
      {...rest}
      value={value}
      onChange={(event: ChangeEvent<HTMLSelectElement>) => onChange(event.target.value)}
      className={cx(
        "h-7 w-full min-w-0 rounded-[4px] border border-hairline bg-canvas px-1.5 text-[12px] text-ink",
        "focus:border-accent focus:outline-none",
        className,
      )}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value} className="bg-surface text-ink">
          {option.label}
        </option>
      ))}
    </select>
  );
}

export function Checkbox({ checked, onChange, label, disabled }: { checked: boolean; onChange: (checked: boolean) => void; label: ReactNode; disabled?: boolean }) {
  return (
    <label className={cx("flex cursor-pointer items-start gap-2 text-[11px] leading-snug text-ink-2", disabled && "cursor-not-allowed opacity-50")}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-[2px] h-3 w-3 shrink-0 rounded-[2px] border border-hairline bg-canvas accent-[#F96316]"
      />
      <span>{label}</span>
    </label>
  );
}

export function Table({ columns, children, className, dense }: { columns: ReactNode[]; children: ReactNode; className?: string; dense?: boolean }) {
  return (
    <div className={cx("nios-scroll min-w-0", className)}>
      <table className="w-full border-collapse text-left text-[11px]">
        <thead>
          <tr className="border-b border-hairline">
            {columns.map((column, index) => (
              <th
                key={index}
                scope="col"
                className={cx(
                  "sticky top-0 z-10 bg-surface font-medium uppercase tracking-[0.1em] text-ink-3",
                  dense ? "px-2 py-1" : "px-2.5 py-1.5",
                  "text-[9.5px] whitespace-nowrap",
                )}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children, className, onClick, tone }: { children: ReactNode; className?: string; onClick?: () => void; tone?: "bull" | "bear" }) {
  return (
    <tr
      onClick={onClick}
      className={cx(
        "border-b border-hairline/70 last:border-b-0 hover:bg-elevated",
        onClick && "cursor-pointer",
        tone === "bull" && "bg-bull-tint/40",
        tone === "bear" && "bg-bear-tint/40",
        className,
      )}
    >
      {children}
    </tr>
  );
}

export function Cell({
  children,
  align = "left",
  tone,
  mono,
  className,
  colSpan,
}: {
  children?: ReactNode;
  align?: "left" | "right" | "center";
  tone?: "neutral" | "bull" | "bear" | "accent" | "muted";
  mono?: boolean;
  className?: string;
  colSpan?: number;
}) {
  const toneClass =
    tone === "bull" ? "text-bull" : tone === "bear" ? "text-bear" : tone === "accent" ? "text-accent" : tone === "muted" ? "text-ink-3" : "text-ink-2";
  return (
    <td
      colSpan={colSpan}
      className={cx(
        "px-2.5 py-1.5 align-middle tabular-nums",
        align === "right" && "text-right",
        align === "center" && "text-center",
        mono && "font-mono text-[10.5px]",
        toneClass,
        className,
      )}
    >
      {children}
    </td>
  );
}

export function KeyValue({ items, columns = 2, className }: { items: Array<{ label: ReactNode; value: ReactNode; tone?: "bull" | "bear" | "accent" | "muted" }>; columns?: number; className?: string }) {
  return (
    <dl className={cx("grid gap-x-4 gap-y-2", columns === 1 ? "grid-cols-1" : columns === 3 ? "grid-cols-3" : "grid-cols-2", className)}>
      {items.map((item, index) => (
        <div key={index} className="flex min-w-0 items-baseline justify-between gap-2 border-b border-hairline/60 pb-1 last:border-b-0">
          <dt className="truncate text-[10px] uppercase tracking-[0.1em] text-ink-3">{item.label}</dt>
          <dd
            className={cx(
              "shrink-0 truncate text-[11px] font-semibold tabular-nums",
              item.tone === "bull" ? "text-bull" : item.tone === "bear" ? "text-bear" : item.tone === "accent" ? "text-accent" : item.tone === "muted" ? "text-ink-3" : "text-ink",
            )}
          >
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function ProgressBar({ value, max = 100, tone = "accent", height = 4, label }: { value: number; max?: number; tone?: "accent" | "bull" | "bear" | "muted"; height?: number; label?: ReactNode }) {
  const safeMax = max <= 0 ? 1 : max;
  const ratio = Math.max(0, Math.min(1, Number.isFinite(value) ? value / safeMax : 0));
  const fill = tone === "bull" ? "bg-bull" : tone === "bear" ? "bg-bear" : tone === "muted" ? "bg-ink-3" : "bg-accent";
  return (
    <div className="min-w-0">
      <div className="w-full overflow-hidden rounded-[2px] bg-elevated" style={{ height }}>
        <div className={cx("h-full rounded-[2px]", fill)} style={{ width: `${(ratio * 100).toFixed(1)}%` }} />
      </div>
      {label !== undefined ? <div className="mt-1 text-[10px] text-ink-3">{label}</div> : null}
    </div>
  );
}

export function Tabs({ tabs, active, onChange, className }: { tabs: Array<{ id: string; label: string; count?: number }>; active: string; onChange: (id: string) => void; className?: string }) {
  return (
    <div className={cx("flex min-w-0 flex-wrap items-center gap-1 border-b border-hairline", className)}>
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={cx(
              "relative -mb-px border-b px-2.5 py-1.5 text-[10.5px] font-medium uppercase tracking-[0.1em] transition-colors",
              selected ? "border-accent text-ink" : "border-transparent text-ink-3 hover:text-ink-2",
            )}
          >
            {tab.label}
            {tab.count !== undefined ? <span className="ml-1.5 tabular-nums text-ink-3">{tab.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

export function EmptyState({ title, hint, action }: { title: ReactNode; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-[4px] border border-dashed border-hairline px-4 py-8 text-center">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-2">{title}</div>
      {hint !== undefined ? <div className="max-w-md text-[11px] text-ink-3">{hint}</div> : null}
      {action}
    </div>
  );
}

export function Spinner({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className="animate-spin text-ink-3" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

export function Banner({ tone = "neutral", children, icon = "info" }: { tone?: "neutral" | "accent" | "bull" | "bear"; children: ReactNode; icon?: IconName }) {
  const styles =
    tone === "bull"
      ? "border-bull-border bg-bull-tint text-bull"
      : tone === "bear"
        ? "border-bear-border bg-bear-tint text-bear"
        : tone === "accent"
          ? "border-[#3A2A1E] bg-[#1E1712] text-accent"
          : "border-hairline bg-surface text-ink-2";
  return (
    <div className={cx("flex items-center gap-2 rounded-[4px] border px-2.5 py-1.5 text-[11px]", styles)}>
      <Icon name={icon} size={13} className="shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function Modal({ open, title, children, onClose, footer, width = 480 }: { open: boolean; title: ReactNode; children: ReactNode; onClose: () => void; footer?: ReactNode; width?: number }) {
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
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-[#0D0E10]/88 px-4 py-10">
      <div className="w-full rounded-[8px] border border-hairline bg-surface" style={{ maxWidth: width }}>
        <div className="flex items-center justify-between border-b border-hairline px-3 py-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-2">{title}</h2>
          <button type="button" onClick={onClose} className="rounded-[4px] p-1 text-ink-3 hover:bg-elevated hover:text-ink" title="Close">
            <Icon name="close" size={13} />
          </button>
        </div>
        <div className="p-3">{children}</div>
        {footer !== undefined ? <div className="flex items-center justify-end gap-2 border-t border-hairline px-3 py-2">{footer}</div> : null}
      </div>
    </div>
  );
}

export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("flex flex-wrap items-end gap-2", className)}>{children}</div>;
}

export function Divider({ className }: { className?: string }) {
  return <div className={cx("h-px w-full bg-hairline", className)} />;
}

export function PageHeader({ title, subtitle, actions, badges }: { title: string; subtitle?: string; actions?: ReactNode; badges?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline pb-2.5">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-[15px] font-semibold tracking-[0.02em] text-ink">{title}</h1>
          {badges}
        </div>
        {subtitle !== undefined ? <p className="mt-0.5 text-[11px] text-ink-3">{subtitle}</p> : null}
      </div>
      {actions !== undefined ? <div className="flex flex-wrap items-center gap-1.5">{actions}</div> : null}
    </div>
  );
}

export function JsonBlock({ value, className }: { value: unknown; className?: string }) {
  let rendered = "";
  try {
    rendered = JSON.stringify(value, null, 2);
  } catch {
    rendered = String(value);
  }
  return (
    <pre className={cx("nios-scroll max-h-72 rounded-[4px] border border-hairline bg-canvas p-2 font-mono text-[10px] leading-relaxed text-ink-2", className)}>
      {rendered}
    </pre>
  );
}

export function StatusDot({ tone = "muted", pulse }: { tone?: "bull" | "bear" | "accent" | "muted"; pulse?: boolean }) {
  const color = tone === "bull" ? "bg-bull" : tone === "bear" ? "bg-bear" : tone === "accent" ? "bg-accent" : "bg-ink-3";
  return <span className={cx("inline-block h-1.5 w-1.5 shrink-0 rounded-[2px]", color, pulse && "animate-pulse")} />;
}
