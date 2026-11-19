/**
 * Shared portal chrome — the four doors into the terminal all speak the same
 * flat institutional language: matte panels, hairlines, no decoration.
 */

import type { ReactNode } from "react";
import { Badge, cx } from "../components/ui";
import { Icon } from "../components/Icon";
import { RING_LEGEND } from "../config/nav";
import { Link } from "../lib/router";

export function PortalFrame({
  eyebrow,
  title,
  subtitle,
  mode = "neutral",
  children,
  footer,
  width = 420,
}: {
  eyebrow: string;
  title: string;
  subtitle: string;
  mode?: "neutral" | "paper" | "live";
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  return (
    <div className="flex min-h-screen bg-canvas text-ink">
      <aside className="hidden w-[44%] shrink-0 flex-col justify-between border-r border-hairline bg-surface px-8 py-8 lg:flex">
        <div>
          <div className="flex items-center gap-2">
            <span className={cx("h-5 w-[2px]", mode === "live" ? "bg-bear" : "bg-accent")} />
            <span className="text-[16px] font-semibold tracking-[0.24em] text-ink">NIOS</span>
          </div>
          <div className="mt-1 text-[9px] uppercase tracking-[0.22em] text-ink-3">Institutional Financial Terminal</div>
          <div className="mt-3 flex items-center gap-1.5">
            <Badge tone="muted">v4.2.0-V2.2</Badge>
            <Badge tone={mode === "live" ? "bear" : mode === "paper" ? "accent" : "neutral"}>
              {mode === "live" ? "LIVE VAULT" : mode === "paper" ? "PAPER SANDBOX" : "SOVEREIGN GATEWAY"}
            </Badge>
          </div>

          <div className="mt-8 space-y-2">
            {RING_LEGEND.map((ring) => (
              <div key={ring.ring} className="flex items-start gap-3 border-b border-hairline pb-2 last:border-b-0">
                <span className="mt-[1px] w-12 shrink-0 text-[9px] font-semibold uppercase tracking-[0.12em] text-ink-3">{ring.ring}</span>
                <div className="min-w-0">
                  <div className="text-[11px] font-medium text-ink-2">{ring.label}</div>
                  <div className="text-[10px] text-ink-3">{ring.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-start gap-2 rounded-[4px] border border-hairline bg-canvas p-2.5">
            <Icon name="shield" size={13} className="mt-[1px] shrink-0 text-ink-3" />
            <p className="text-[10px] leading-relaxed text-ink-3">
              Nothing reaches LIVE until every invariant is certified. LLMs reason about the market; they never enter the
              microsecond execution loop.
            </p>
          </div>
          <div className="flex items-center justify-between text-[9px] uppercase tracking-[0.16em] text-ink-3">
            <span>sec_016 · certified cutover</span>
            <Link to="/login" className="text-ink-3 hover:text-ink-2">
              GATEWAY
            </Link>
          </div>
        </div>
      </aside>

      <main className="flex flex-1 items-center justify-center px-5 py-10">
        <div className="w-full" style={{ maxWidth: width }}>
          <div className="text-[9px] font-semibold uppercase tracking-[0.22em] text-ink-3">{eyebrow}</div>
          <h1 className="mt-1.5 text-[20px] font-semibold leading-tight tracking-[0.01em] text-ink">{title}</h1>
          <p className="mt-1 text-[11px] leading-relaxed text-ink-3">{subtitle}</p>
          <div className="mt-6 rounded-[6px] border border-hairline bg-surface p-4">{children}</div>
          {footer !== undefined ? <div className="mt-3">{footer}</div> : null}
          <div className="mt-6 flex items-center justify-between text-[9px] uppercase tracking-[0.16em] text-ink-3">
            <span>FastAPI · REST + SSE · air-gapped Ring 2</span>
            <span>UTC</span>
          </div>
        </div>
      </main>
    </div>
  );
}

export function PortalError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="mb-3 rounded-[4px] border border-bear-border bg-bear-tint px-2.5 py-2 text-[11px] leading-snug text-bear">
      {message}
    </div>
  );
}

export function PortalSubmit({ label, disabled, onClick }: { label: string; disabled?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="mt-1 flex h-8 w-full items-center justify-center gap-2 rounded-[4px] border border-accent bg-accent text-[11px] font-semibold uppercase tracking-[0.14em] text-[#0D0E10] hover:bg-[#E85A12] disabled:opacity-40"
    >
      {label}
      <Icon name="arrowRight" size={13} />
    </button>
  );
}
